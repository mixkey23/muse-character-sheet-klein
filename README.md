# Muse Character Sheet — Klein

[FLUX.2 \[klein\]](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B) is Black Forest Labs' fast, step-distilled image generation/editing model. **Muse Character Sheet — Klein** is a single ComfyUI node that generates a full 5-pose character turnaround (portrait, front, left profile, right profile, back) from **one character reference photo** — no separate mannequin guide sheet required, poses are described directly in text.

Instead of wiring up five separate Klein generations by hand, the node runs all five internally and gives you **Confirm** / **New seed** / **Apply edit** buttons per pose in its own panel, plus an **Edit all poses** box to apply one instruction to every unconfirmed pose at once. Confirm is a pure local lock — it doesn't submit anything on its own. "New seed" on a pose with an active edit re-rolls *that edit*, not the original. A **seed_mode** widget (`random`/`fixed`) controls whether re-running the same photo after a completed sheet gives you a fresh random set or a reproducible one. Once every pose is confirmed, click **Build final sheet now** to assemble the final 4096x2304 sheet.

You can optionally connect a second **pose_reference_image** as a purely structural pose anchor — it's never mentioned in the prompt text, just chained in as an extra reference to help hold a pose.

## ⚠️ Required custom nodes — install these BEFORE you run anything

FLUX.2 support (`ReferenceLatent`, `EmptyFlux2LatentImage`, `FluxKVCache`, etc.) ships in ComfyUI core — nothing extra to install for the generation pipeline itself.

- **[ComfyUI-RMBG](https://github.com/1038lab/ComfyUI-RMBG)** — required for the white-background cleanup run on every pose. The RMBG-2.0 model it uses auto-downloads on first use.
- **[ComfyUI-Impact-Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack)** + **[ComfyUI-Impact-Subpack](https://github.com/ltdrdata/ComfyUI-Impact-Subpack)** — only required if you enable the node's **Face Detail** pass (off by default).

This repo also bundles **Muse Sheet: Align Figure Height** (`MuseSheetAlignFigure`), used internally to size/align every panel in the final sheet — no separate install needed.

## ⚖️ Model licensing — read before picking a checkpoint

- **9B / 9B-KV** — [FLUX Non-Commercial License](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/blob/main/LICENSE.md). Free to download and use for personal research/testing. Per [Black Forest Labs' own licensing FAQ](https://bfl.ai/licensing): *"If you're generating images for products you charge for, creating assets for clients, or incorporating model weights into software you distribute, you need a commercial license."* If you're using this for paid/monetized content, you need BFL's Commercial Weights License (their **Builder** tier fits a small team generating marketing/content visuals).
- **4B** — [Apache 2.0](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) — "Open weights available for commercial use." No restrictions, no commercial license needed. Lighter (~13GB VRAM vs ~29GB for 9B), slightly lower quality ceiling.

## Model Links

### Diffusion model (`unet_name` widget) — pick ONE

**9B-KV** (recommended for quality; requires a commercial license for monetized use — see above)
[🤗 black-forest-labs/FLUX.2-klein-9b-kv-fp8](https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-kv-fp8)
- [flux-2-klein-9b-kv-fp8.safetensors](https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-kv-fp8/resolve/main/flux-2-klein-9b-kv-fp8.safetensors) (9.82 GB)

**4B** (fully open, Apache 2.0, no commercial restrictions)
[🤗 Comfy-Org/vae-text-encorder-for-flux-klein-4b](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/tree/main/split_files/diffusion_models)
- [flux-2-klein-4b.safetensors](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/diffusion_models/flux-2-klein-4b.safetensors) (7.75 GB) — distilled/fast variant, use this one
- flux-2-klein-base-4b.safetensors (7.75 GB) — non-distilled "base" variant, also available in the same folder if you want to experiment

### Text encoder (`clip_name`, type `flux2`) — match whichever diffusion model you picked

**For 9B-KV**: [🤗 Comfy-Org/vae-text-encorder-for-flux-klein-9b](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/tree/main/split_files/text_encoders)
- [qwen_3_8b_fp8mixed.safetensors](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors) (8.66 GB) — recommended balance
- qwen_3_8b.safetensors (16.4 GB, full precision) / qwen_3_8b_fp4mixed.safetensors (6.8 GB, lighter) also available in the same folder

**For 4B**: [🤗 Comfy-Org/vae-text-encorder-for-flux-klein-4b](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/tree/main/split_files/text_encoders)
- [qwen_3_4b.safetensors](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors) (8.04 GB), or qwen_3_4b_fp4_flux2.safetensors (3.85 GB, lighter)

### VAE (`vae_name`) — same file regardless of which model you picked

- [flux2-vae.safetensors](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/vae/flux2-vae.safetensors) (336 MB)

No LoRAs required.

## `kv_cache` widget

`FluxKVCache` is a speed optimization built specifically for the 9B-KV checkpoint's multi-reference caching — it produces broken/distorted output on the 4B model, which has no KV-cache support. The `kv_cache` widget (`auto` / `on` / `off`, default `auto`) detects this from the `unet_name` filename automatically, so in almost all cases you don't need to touch it. Set it to `off` manually if you're loading the 4B model through a `model_override` socket instead of the `unet_name` dropdown (auto-detection needs the filename, which an already-loaded MODEL doesn't carry).

## Model Storage Locations

- `ComfyUI/models/diffusion_models/` — `flux-2-klein-9b-kv-fp8.safetensors` and/or `flux-2-klein-4b.safetensors`
- `ComfyUI/models/text_encoders/` — `qwen_3_8b_fp8mixed.safetensors` and/or `qwen_3_4b.safetensors`
- `ComfyUI/models/vae/` — `flux2-vae.safetensors`
- `ComfyUI/models/ultralytics/bbox/` — `face_yolov8m.pt` *(only needed if you enable Face Detail; ships/auto-downloads with Impact-Subpack)*

## Links
- [FLUX.2 klein on Hugging Face (9B)](https://huggingface.co/black-forest-labs/FLUX.2-klein-9B)
- [FLUX.2 klein 4B (Apache 2.0)](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)
- [Black Forest Labs licensing](https://bfl.ai/licensing)
- [Muse Character Sheet (Krea2 version) on GitHub](https://github.com/muse-collective-26/muse-character-sheet)
- [Muse Model Loader on GitHub](https://github.com/muse-collective-26/muse-model-loader)
