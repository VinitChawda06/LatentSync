# Adapted from https://github.com/guoyww/AnimateDiff/blob/main/animatediff/pipelines/pipeline_animation.py

import inspect
import os
import shutil
from typing import Callable, List, Optional, Union
import subprocess

import numpy as np
import torch
import torchvision

from diffusers.utils import is_accelerate_available
from packaging import version

from diffusers.configuration_utils import FrozenDict
from diffusers.models import AutoencoderKL
from diffusers.pipeline_utils import DiffusionPipeline
from diffusers.schedulers import (
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
)
from diffusers.utils import deprecate, logging

from einops import rearrange
import cv2

from ..models.unet import UNet3DConditionModel
from ..utils.image_processor import ImageProcessor
from ..utils.util import read_video, read_audio, write_video, check_ffmpeg_installed
from ..whisper.audio2feature import Audio2Feature
import tqdm
import soundfile as sf

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


class LipsyncPipeline(DiffusionPipeline):
    _optional_components = []

    def __init__(
        self,
        vae: AutoencoderKL,
        audio_encoder: Audio2Feature,
        unet: UNet3DConditionModel,
        scheduler: Union[
            DDIMScheduler,
            PNDMScheduler,
            LMSDiscreteScheduler,
            EulerDiscreteScheduler,
            EulerAncestralDiscreteScheduler,
            DPMSolverMultistepScheduler,
        ],
    ):
        super().__init__()

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1. Please update the config."
            )
            deprecate("steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is True:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has `clip_sample`=True."
                " Should be set to False. Please update the config."
            )
            deprecate("clip_sample not set", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        is_unet_version_less_0_9_0 = hasattr(unet.config, "_diffusers_version") and version.parse(
            version.parse(unet.config._diffusers_version).base_version
        ) < version.parse("0.9.0.dev0")
        is_unet_sample_size_less_64 = hasattr(unet.config, "sample_size") and unet.config.sample_size < 64
        if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
            deprecation_message = (
                "The configuration file of the unet has `sample_size` < 64 which is unlikely. "
                "If your checkpoint is fine-tuned from stable-diffusion, set sample_size=64."
            )
            deprecate("sample_size<64", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(unet.config)
            new_config["sample_size"] = 64
            unet._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            audio_encoder=audio_encoder,
            unet=unet,
            scheduler=scheduler,
        )

        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.set_progress_bar_config(desc="Steps")

    def enable_vae_slicing(self):
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError("Please install accelerate via `pip install accelerate`")

        device = torch.device(f"cuda:{gpu_id}")

        for cpu_offloaded_model in [self.unet, self.vae, self.audio_encoder]:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

    @property
    def _execution_device(self):
        if self.device != torch.device("meta") or not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                hasattr(module, "_hf_hook")
                and hasattr(module._hf_hook, "execution_device")
                and module._hf_hook.execution_device is not None
            ):
                return torch.device(module._hf_hook.execution_device)
        return self.device

    def decode_latents(self, latents):
        latents = latents / self.vae.config.scaling_factor + self.vae.config.shift_factor
        latents = rearrange(latents, "b c f h w -> (b f) c h w")
        decoded_latents = self.vae.decode(latents).sample
        return decoded_latents

    def prepare_extra_step_kwargs(self, generator, eta):
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(self, height, width, callback_steps):
        assert height == width, "Height and width must be equal"
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` must be divisible by 8 but are {height} x {width}.")

        if (callback_steps is None) or (not isinstance(callback_steps, int) or callback_steps <= 0):
            raise ValueError(f"`callback_steps` must be a positive integer but is {callback_steps}.")

    def prepare_latents(self, batch_size, num_frames, num_channels_latents, height, width, dtype, device, generator):
        shape = (
            batch_size,
            num_channels_latents,
            1,
            height // self.vae_scale_factor,
            width // self.vae_scale_factor,
        )
        rand_device = "cpu" if device.type == "mps" else device
        latents = torch.randn(shape, generator=generator, device=rand_device, dtype=dtype).to(device)
        latents = latents.repeat(1, 1, num_frames, 1, 1)
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    def prepare_mask_latents(self, mask, masked_image, height, width, dtype, device, generator, do_classifier_free_guidance):
        mask = torch.nn.functional.interpolate(
            mask, size=(height // self.vae_scale_factor, width // self.vae_scale_factor)
        )
        masked_image = masked_image.to(device=device, dtype=dtype)
        masked_image_latents = self.vae.encode(masked_image).latent_dist.sample(generator=generator)
        masked_image_latents = (masked_image_latents - self.vae.config.shift_factor) * self.vae.config.scaling_factor

        masked_image_latents = masked_image_latents.to(device=device, dtype=dtype)
        mask = mask.to(device=device, dtype=dtype)

        mask = rearrange(mask, "f c h w -> 1 c f h w")
        masked_image_latents = rearrange(masked_image_latents, "f c h w -> 1 c f h w")

        if do_classifier_free_guidance:
            mask = torch.cat([mask] * 2)
            masked_image_latents = torch.cat([masked_image_latents] * 2)
        return mask, masked_image_latents

    def prepare_image_latents(self, images, device, dtype, generator, do_classifier_free_guidance):
        images = images.to(device=device, dtype=dtype)
        image_latents = self.vae.encode(images).latent_dist.sample(generator=generator)
        image_latents = (image_latents - self.vae.config.shift_factor) * self.vae.config.scaling_factor
        image_latents = rearrange(image_latents, "f c h w -> 1 c f h w")
        if do_classifier_free_guidance:
            image_latents = torch.cat([image_latents] * 2)
        return image_latents

    def set_progress_bar_config(self, **kwargs):
        if not hasattr(self, "_progress_bar_config"):
            self._progress_bar_config = {}
        self._progress_bar_config.update(kwargs)

    @staticmethod
    def paste_surrounding_pixels_back(decoded_latents, pixel_values, masks, device, weight_dtype):
        pixel_values = pixel_values.to(device=device, dtype=weight_dtype)
        masks = masks.to(device=device, dtype=weight_dtype)
        combined_pixel_values = decoded_latents * masks + pixel_values * (1 - masks)
        return combined_pixel_values

    @staticmethod
    def pixel_values_to_images(pixel_values: torch.Tensor):
        pixel_values = rearrange(pixel_values, "f c h w -> f h w c")
        pixel_values = (pixel_values / 2 + 0.5).clamp(0, 1)
        images = (pixel_values * 255).to(torch.uint8)
        images = images.cpu().numpy()
        return images

    def affine_transform_video(self, video_path):
        video_frames = read_video(video_path, use_decord=False)
        faces = []
        boxes = []
        affine_matrices = []
        print(f"Affine transforming {len(video_frames)} faces...")
        for frame in tqdm.tqdm(video_frames):
            face, box, affine_matrix = self.image_processor.affine_transform(frame)
            faces.append(face)
            boxes.append(box)
            affine_matrices.append(affine_matrix)
        faces = torch.stack(faces)
        return faces, video_frames, boxes, affine_matrices

    def restore_video(self, faces, video_frames, boxes, affine_matrices):
        """
        Replaces the lipsynced faces into the original frames.
        Potential place to do super-resolution on the face patch if needed.
        """
        video_frames = video_frames[: faces.shape[0]]
        out_frames = []
        print(f"Restoring {len(faces)} faces...")
        for index, face in enumerate(tqdm.tqdm(faces)):
            x1, y1, x2, y2 = boxes[index]
            height = int(y2 - y1)
            width = int(x2 - x1)

            # face shape = (C, H, W)
            _, face_h, face_w = face.shape

            # === SUPERRES ADD START ===
            # If user selected GFPGAN/CodeFormer AND the face is smaller than the region
            # we are about to fill, run superresolution. 
            if self.superres != "none" and (face_h < height or face_w < width):
                scale_h = height / face_h
                scale_w = width / face_w
                scale_factor = max(scale_h, scale_w)
                # Convert face (torch tensor) to a NumPy or PIL image, apply SR, convert back:
                if self.superres == "GFPGAN":
                    face = self.apply_gfpgan_superres(face, scale_factor)
                elif self.superres == "CodeFormer":
                    face = self.apply_codeformer_superres(face, scale_factor)
                # else, default do nothing special
            # === SUPERRES ADD END ===

            # Now do normal resize to match bounding box
            face = torchvision.transforms.functional.resize(face, size=(height, width), antialias=True)
            face = rearrange(face, "c h w -> h w c")
            face = (face / 2 + 0.5).clamp(0, 1)
            face = (face * 255).to(torch.uint8).cpu().numpy()

            # If your pipeline already does face restoration or alignment:
            out_frame = self.image_processor.restorer.restore_img(
                video_frames[index], face, affine_matrices[index]
            )
            out_frames.append(out_frame)
        return np.stack(out_frames, axis=0)

    # === SUPERRES ADD START ===
    def apply_gfpgan_superres(self, face_tensor, scale_factor):
        """
        Example placeholder for GFPGAN super-resolution logic.
        - face_tensor is (C, H, W) in [0,1] or [-1,1], depending on pipeline
        - scale_factor is a float ratio

        You must convert to a format GFPGAN expects (e.g. BGR NumPy image),
        run GFPGAN's restorer, then convert back to the shape (C, H, W).
        """
        # Convert Torch -> NumPy for demonstration
        face_np = face_tensor.detach().cpu().float().numpy()  # shape (C,H,W)
        face_np = np.transpose(face_np, (1, 2, 0))  # (H,W,C)

        # For example, scale up with cv2 first
        new_h = int(face_np.shape[0] * scale_factor)
        new_w = int(face_np.shape[1] * scale_factor)
        face_np_bgr = cv2.cvtColor(face_np, cv2.COLOR_RGB2BGR)
        upscaled = cv2.resize(face_np_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Here you would run GFPGAN's restorer. For example:
        #   _, restored_bgr, _ = self.gfpgan_restorer.enhance(upscaled, has_aligned=True, only_center_face=False)
        # Suppose we skip and just pretend 'restored_bgr = upscaled' 
        restored_bgr = upscaled

        # Convert back
        restored_rgb = cv2.cvtColor(restored_bgr, cv2.COLOR_BGR2RGB)
        restored_rgb = np.transpose(restored_rgb, (2, 0, 1))  # (C,H,W)
        # Convert to Torch
        restored_torch = torch.from_numpy(restored_rgb).to(face_tensor.device, dtype=face_tensor.dtype)

        # If your pipeline expects -1..1, rescale:
        #   restored_torch = (restored_torch / 255.0) * 2 - 1
        # Or if your pipeline uses 0..1, do that. Example:
        restored_torch = restored_torch / 255.0  # 0..1
        return restored_torch

    def apply_codeformer_superres(self, face_tensor, scale_factor):
        """
        Example placeholder for CodeFormer super-resolution logic.
        Same steps: convert face_tensor -> NumPy -> run CodeFormer -> back to torch.
        """
        face_np = face_tensor.detach().cpu().float().numpy()
        face_np = np.transpose(face_np, (1, 2, 0))  # (H,W,C)
        new_h = int(face_np.shape[0] * scale_factor)
        new_w = int(face_np.shape[1] * scale_factor)
        face_np_bgr = cv2.cvtColor(face_np, cv2.COLOR_RGB2BGR)
        upscaled = cv2.resize(face_np_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Here you would do something like: result_bgr = codeformer_enhancer(upscaled)
        # For now, assume we skip and do no real enhancement:
        result_bgr = upscaled

        restored_rgb = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB)
        restored_rgb = np.transpose(restored_rgb, (2, 0, 1))
        restored_torch = torch.from_numpy(restored_rgb).to(face_tensor.device, dtype=face_tensor.dtype)
        restored_torch = restored_torch / 255.0  # 0..1 range
        return restored_torch
    # === SUPERRES ADD END ===

    @torch.no_grad()
    def __call__(
        self,
        video_path: str,
        audio_path: str,
        video_out_path: str,
        video_mask_path: str = None,
        num_frames: int = 16,
        video_fps: int = 25,
        audio_sample_rate: int = 16000,
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_inference_steps: int = 20,
        guidance_scale: float = 1.5,
        weight_dtype: Optional[torch.dtype] = torch.float16,
        eta: float = 0.0,
        mask: str = "fix_mask",
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: Optional[int] = 1,
        superres: str = "none",  # <--- ADDED
        **kwargs,
    ):
        """
        superres: "none", "GFPGAN", or "CodeFormer"
        """
        # Store the superres option for usage in restore_video()
        self.superres = superres

        is_train = self.unet.training
        self.unet.eval()

        check_ffmpeg_installed()

        batch_size = 1
        device = self._execution_device
        self.image_processor = ImageProcessor(height, mask=mask, device="cuda")
        self.set_progress_bar_config(desc=f"Sample frames: {num_frames}")

        faces, original_video_frames, boxes, affine_matrices = self.affine_transform_video(video_path)
        audio_samples = read_audio(audio_path)

        # 1. Default height and width
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # 2. Check inputs
        self.check_inputs(height, width, callback_steps)

        do_classifier_free_guidance = guidance_scale > 1.0
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        self.video_fps = video_fps

        if self.unet.add_audio_layer:
            whisper_feature = self.audio_encoder.audio2feat(audio_path)
            whisper_chunks = self.audio_encoder.feature2chunks(feature_array=whisper_feature, fps=video_fps)
            num_inferences = min(len(faces), len(whisper_chunks)) // num_frames
        else:
            num_inferences = len(faces) // num_frames

        synced_video_frames = []
        num_channels_latents = self.vae.config.latent_channels
        all_latents = self.prepare_latents(
            batch_size,
            num_frames * num_inferences,
            num_channels_latents,
            height,
            width,
            weight_dtype,
            device,
            generator,
        )

        for i in tqdm.tqdm(range(num_inferences), desc="Doing inference..."):
            if self.unet.add_audio_layer:
                audio_embeds = torch.stack(whisper_chunks[i * num_frames : (i + 1) * num_frames])
                audio_embeds = audio_embeds.to(device, dtype=weight_dtype)
                if do_classifier_free_guidance:
                    null_audio_embeds = torch.zeros_like(audio_embeds)
                    audio_embeds = torch.cat([null_audio_embeds, audio_embeds])
            else:
                audio_embeds = None

            inference_faces = faces[i * num_frames : (i + 1) * num_frames]
            latents = all_latents[:, :, i * num_frames : (i + 1) * num_frames]

            pixel_values, masked_pixel_values, masks = self.image_processor.prepare_masks_and_masked_images(
                inference_faces, affine_transform=False
            )

            mask_latents, masked_image_latents = self.prepare_mask_latents(
                masks,
                masked_pixel_values,
                height,
                width,
                weight_dtype,
                device,
                generator,
                do_classifier_free_guidance,
            )

            image_latents = self.prepare_image_latents(
                pixel_values,
                device,
                weight_dtype,
                generator,
                do_classifier_free_guidance,
            )

            num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
            with self.progress_bar(total=num_inference_steps) as progress_bar:
                for j, t in enumerate(timesteps):
                    latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                    latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
                    latent_model_input = torch.cat(
                        [latent_model_input, mask_latents, masked_image_latents, image_latents], dim=1
                    )
                    noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=audio_embeds).sample

                    if do_classifier_free_guidance:
                        noise_pred_uncond, noise_pred_audio = noise_pred.chunk(2)
                        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_audio - noise_pred_uncond)

                    latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

                    if j == len(timesteps) - 1 or ((j + 1) > num_warmup_steps and (j + 1) % self.scheduler.order == 0):
                        progress_bar.update()
                        if callback is not None and j % callback_steps == 0:
                            callback(j, t, latents)

            decoded_latents = self.decode_latents(latents)
            decoded_latents = self.paste_surrounding_pixels_back(
                decoded_latents, pixel_values, 1 - masks, device, weight_dtype
            )
            synced_video_frames.append(decoded_latents)

        # Combine and restore the faces onto the original frames
        synced_video_frames = self.restore_video(
            torch.cat(synced_video_frames), original_video_frames, boxes, affine_matrices
        )

        audio_samples_remain_length = int(synced_video_frames.shape[0] / video_fps * audio_sample_rate)
        audio_samples = audio_samples[:audio_samples_remain_length].cpu().numpy()

        if is_train:
            self.unet.train()

        temp_dir = "temp"
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

        write_video(os.path.join(temp_dir, "video.mp4"), synced_video_frames, fps=25)
        sf.write(os.path.join(temp_dir, "audio.wav"), audio_samples, audio_sample_rate)

        command = f"ffmpeg -y -loglevel error -nostdin -i {os.path.join(temp_dir, 'video.mp4')} -i {os.path.join(temp_dir, 'audio.wav')} -c:v libx264 -c:a aac -q:v 0 -q:a 0 {video_out_path}"
        subprocess.run(command, shell=True)
