"""Man4Tech Character Sheet (Klein).

Sibling of MuseCharacterSheetDirector (Muse-CharacterSheet-Director), same
confirm/re-roll session-state architecture, but generates each pose with
FLUX.2 [klein] as a single-reference edit model instead of Krea2Edit as a
two-reference (guide + character) edit model. There is deliberately no
guide_image input here - Klein poses come from describing the target pose
directly in text (see POSE_PROMPTS), anchored to the one character_image
reference for identity/outfit, not from cropping a mannequin guide sheet.

Architecture note (same hard platform constraint as the Krea2 node): Python
can only act from inside a real node execution triggered by a /prompt
submission - every user action (initial run, re-roll one pose, finalize) is
its own small /prompt submission from the JS side; state that must survive
between those submissions rides in the hidden `state_json` widget plus an
in-process cache here keyed by the node's unique_id.
"""
import hashlib
import json
import random

import torch

import comfy.model_management
import comfy.samplers
import comfy.utils
import folder_paths
import nodes as comfy_nodes
from comfy_execution.graph import ExecutionBlocker

POSE_NAMES = ["01_portrait", "02_front", "03_left_profile", "04_right_profile", "05_back"]
POSE_LABELS = ["Portrait (close-up)", "Front", "Left profile", "Right profile", "Back"]
DEFAULT_SEEDS = [41001, 41002, 41003, 41004, 41005]

# [2026-09-19] Target (latent-space) pixel resolution per pose - independent of
# whatever aspect ratio the character reference photo happens to be, so the
# final sheet's panels stay consistent regardless of the source image. Same
# proportions as the Krea2 node's own TARGET_SIZE, but rounded to multiples of
# 16 (FLUX.2's own resolution requirement - see BFL's prompting guide: "Output
# dimensions must be multiples of 16").
# [2026-09-22] Left/right profile narrowed from 544 to 416 (still a multiple
# of 16) - a true side-on silhouette is only chest-to-back deep, much
# narrower than a front-on shoulder-to-shoulder view, so sharing the front
# pose's canvas width was making the model widen/stockify the body to fill
# the frame (Andy: "they don't look like they represent the normal height of
# the person"). Height is unchanged, so ALIGN_BODY_KW's fixed figure_height/
# bottom_margin still line every panel up at the same final height in
# _assemble_final - only the pre-alignment generation canvas is narrower.
# char_latent/pose_latent are encoded at their own native resolution and
# chained in via ReferenceLatent regardless of target_latent's size (that's
# the whole point of Flux's reference-latent mechanism), so no other
# constant needs to change alongside this.
TARGET_SIZE = {
    "01_portrait": (544, 976),
    "02_front": (544, 1792),
    "03_left_profile": (416, 1792),
    "04_right_profile": (416, 1792),
    "05_back": (544, 1792),
}

ALIGN_BODY_KW = dict(width=704, height=2304, figure_height=2048, bottom_margin=128, threshold=0.1)
PORTRAIT_FINAL_SIZE = (1280, 2304)  # (width, height)

# [2026-09-24] Per-figure_type overrides for ALIGN_BODY_KW's figure_height/
# bottom_margin ONLY - width/height/threshold stay identical across types.
# MuseSheetAlignFigure (sheet_align.py, untouched) already normalizes off
# the REAL bbox of the RMBG mask, not an anatomy assumption - the only
# figure-agnostic gap was these two fixed numbers, tuned for a human
# standing tall with feet near the bottom margin. Absent from this dict ==
# identical to today's behavior (human/humanoid/animal/creature/robot/
# object/custom all fall back to ALIGN_BODY_KW's own defaults via .get()).
FIGURE_TYPE_ALIGN_OVERRIDES = {
    "quadruped": dict(figure_height=1536, bottom_margin=384),
    "floating": dict(figure_height=1792, bottom_margin=256),
}


def _align_kw_for(figure_type, scale):
    base = {**ALIGN_BODY_KW, **FIGURE_TYPE_ALIGN_OVERRIDES.get(figure_type, {})}
    return {**base,
            "width": round(base["width"] * scale),
            "height": round(base["height"] * scale),
            "figure_height": round(base["figure_height"] * scale),
            "bottom_margin": round(base["bottom_margin"] * scale)}

# [2026-09-17] pose_reference_image crop rects - identical to the Krea2
# sibling node's own GUIDE_CROPS (same mannequin template layout, verified
# against Codex's original manual "KREA 2 Character Sheet - Pose Guides"
# workflow, byte-identical numbers). Added because the previous approach -
# feeding the WHOLE 5-panel sheet as one reference, unchanged, for all 5
# generations - relied entirely on the model's own vision-grounding to
# figure out which figure in the busy multi-pose sheet the text prompt's
# pose description meant, with no explicit pointer to the right region. That
# is a real ask, not a guarantee. Cropping to just the ONE relevant panel per
# generation removes the ambiguity outright: if only one pose is visible,
# there's nothing else for the grounding to mistakenly latch onto (Andy:
# "if it only could see a specific angle, then that is the grounding and it
# should react to that").
REFERENCE_GUIDE_SIZE = (1670, 942)  # (width, height)
GUIDE_CROPS = {
    "01_portrait":      dict(x=0,    y=0, width=508, height=942),
    "02_front":         dict(x=508,  y=0, width=307, height=942),
    "04_right_profile": dict(x=815,  y=0, width=237, height=942),
    "03_left_profile":  dict(x=1052, y=0, width=267, height=942),
    "05_back":          dict(x=1319, y=0, width=351, height=942),
}
# Same proven proportions as the Krea2 node's own ALIGN_GUIDE_KW - normalizes
# each crop's figure height/placement before it's encoded, independent of
# TARGET_SIZE (ReferenceLatent doesn't require matching the target's shape).
ALIGN_POSE_REF_KW = dict(width=544, height=1784, figure_height=1584, bottom_margin=100, threshold=0.1)

# [2026-09-17] Named presets showing the actual final pixel size, not a bare
# scale multiplier - "1.3x" tells you nothing about what you'll get, and
# there's no way to reverse-engineer a target size from it without doing the
# maths yourself (Andy: "who's supposed to know that's the default size").
# Every panel's proportions stay identical across presets (see
# _assemble_final) - the character sheet is still a fixed, exact size, you're
# just picking which fixed size.
OUTPUT_SIZE_PRESETS = {
    "4096x2304 (Standard - default)": 1.0,
    "3072x1728 (Medium)": 0.75,
    "2048x1152 (Small)": 0.5,
    "1536x864 (Extra Small)": 0.375,
}

# [2026-09-19] FLUX.2 doesn't support negative prompts at all (BFL's own
# prompting guide: "No negative prompts: FLUX.2 does not support negative
# prompts. Focus on describing what you want, not what you don't want.") -
# there's no NEGATIVE_TEXT/per-pose-negative concept here the way the Krea2
# node needed one. The "negative" conditioning below is just FLUX.2's own
# zeroed-out convention (ConditioningZeroOut), a structural requirement of the
# ReferenceLatent/KSampler wiring, not a place to put steering text.

# [2026-09-23] FS-005: canonical view vocabulary, defined purely by CAMERA
# RELATIONSHIP to the subject - never by anatomy. This is what lets FS-004
# strip human-specific language ("arms relaxed by their sides", "shoulders
# square") out of POSE_PROMPTS without losing what each view actually means:
# a quadruped, a floating creature or a robot all have a "front" (the camera
# facing whatever the subject treats as its front) and a "back" (directly
# opposite), even though neither has "shoulders" or "arms". `crop` is only
# set for the one view that isn't a full-body framing.
VIEW_DEFINITIONS = {
    "01_portrait": {
        "camera": "facing the subject's identifying front end",
        "crop": "close-up, cropped tightly to the subject's identifying front end only",
    },
    "02_front": {"camera": "facing the subject's front", "crop": None},
    "03_left_profile": {"camera": "at the subject's left side, 90 degrees from the front", "crop": None},
    "04_right_profile": {"camera": "at the subject's right side, 90 degrees from the front", "crop": None},
    "05_back": {"camera": "directly behind the subject, opposite the front", "crop": None},
}

# [2026-09-24] Per-figure_type "presentation" clause - the one piece of
# POSE_PROMPTS that genuinely differs by body plan (a quadruped can't have a
# "standing presentation" the same way a biped does). Deliberately just a
# short clause, not a full per-type anatomy vocabulary - the rest of the
# prompt (camera relationship, "unchanged identity/coloring/markings") stays
# identical across every type. "custom" is intentionally absent here - it
# has no preset, see _build_pose_prompts/_is_unedited_default below.
FIGURE_TYPE_PRESENTATION = {
    "human": "neutral standing presentation",
    "humanoid": "neutral standing presentation",
    "quadruped": "neutral four-legged standing presentation",
    "animal": "neutral natural resting presentation",
    "creature": "neutral natural resting presentation",
    "robot": "neutral standing presentation",
    "object": "neutral resting presentation",
    "floating": "neutral suspended presentation",
}
FIGURE_TYPE_CHOICES = list(FIGURE_TYPE_PRESENTATION) + ["custom"]

# [2026-09-24] FS-004 (audit fix): "The subject" -> "The character" per the
# audit's explicit ask; framing/closing clauses reworded closer to the
# audit's own suggested direction ("Same identity, coloring, markings, and
# outfit/materials unchanged"). Still camera-relationship-only, still no
# anatomy words (no "arms", "shoulders", "feet", "face/hairstyle/skin tone").
# [2026-09-23] FS-005: rebuilt from VIEW_DEFINITIONS - "the character in
# image 1" anchors identity to the one reference without assuming a body
# plan; "full body visible" reads fine for a quadruped, a floating figure or
# a robot without singling out limbs, faces or hair.
# [2026-09-19] Single-reference, text-described poses - proven in real testing
# against this exact workflow (Flux Klein 1 Image Ref.json). Everything is a
# concrete, generic pose description with no image-specific detail, so the
# same prompt works for any character photo dropped in - see Andy's explicit
# requirement that this NOT be hardcoded to one specific photo's content.
def _view_prompt(pose_name, presentation):
    view = VIEW_DEFINITIONS[pose_name]
    if view["crop"]:
        framing = view["crop"]
    else:
        framing = "full body visible"
    return (
        f"The character in image 1, {framing}, camera {view['camera']}, "
        f"{presentation}, on a plain white background. Same identity, coloring, "
        f"markings, and outfit/materials unchanged."
    )


def _build_pose_prompts(figure_type):
    """Returns the {pose_name: prompt} preset for one figure_type. Unknown
    values (including "custom", which has no preset by design) fall back to
    "human" - safe because this is only ever used to produce a CONCRETE
    default text, never to silently guess what "custom" should mean."""
    presentation = FIGURE_TYPE_PRESENTATION.get(figure_type, FIGURE_TYPE_PRESENTATION["human"])
    return {name: _view_prompt(name, presentation) for name in POSE_NAMES}


# [2026-09-24] One preset per real figure_type, computed once - used both as
# the base for _build_pose_prompts() and to detect "this pose's prompt is
# still exactly some preset's default text, not hand-edited" (see
# _is_unedited_default). "custom" has no entry here on purpose.
_FIGURE_TYPE_PROMPT_PRESETS = {ft: _build_pose_prompts(ft) for ft in FIGURE_TYPE_PRESENTATION}


def _is_unedited_default(pose_name, text):
    return any(text == presets[pose_name] for presets in _FIGURE_TYPE_PROMPT_PRESETS.values())


POSE_PROMPTS = _FIGURE_TYPE_PROMPT_PRESETS["human"]

RMBG_KW = dict(
    model="RMBG-2.0", sensitivity=1.0, process_res=1024, mask_blur=0, mask_offset=0,
    invert_output=False, refine_foreground=False, background="Color", background_color="#ffffff",
)

DEFAULT_PROMPTS = [POSE_PROMPTS[name] for name in POSE_NAMES]
DEFAULT_STATE = {
    "seeds": list(DEFAULT_SEEDS), "confirmed": [False] * 5, "action": None,
    "prompts": list(DEFAULT_PROMPTS),
}

# Face-detail (Impact Pack FaceDetailer) pass, run on each pose right after decode.
# Identical mechanism to the Krea2 node's - reused as-is, model-family-agnostic.
FACE_DETAIL_DETECTOR_MODELS = {
    "face": "bbox/face_yolov8m.pt",
    "hand": "bbox/hand_yolov8s.pt",
    "person": "segm/person_yolov8m-seg.pt",
}
FACE_DETAIL_STEPS = 4
# [2026-09-20] bbox_threshold 0.50 -> 0.75 and drop_size 10 -> 40 after a
# first malfunction: on one FLUX.2 [klein] generation, the face detector
# returned 300 "faces" in a single 640x224 image (a real face there fills a
# large fraction of the frame, per the SAME run's portrait-pose detection:
# "640x384 1 face").
# [2026-09-17] That threshold tightening reduced false positives on most
# poses but did NOT hold on every image - confirmed live, the exact same
# 300-detection storm recurred on a different pose after the fix. The real
# problem was architectural: FaceDetailer has no built-in cap on how many
# detected regions it refines, full stop - it dutifully ran a full
# encode/sample/decode pass for every single one, which is what an "is this
# run going mad? we're on 76/96 now" report was actually seeing both times.
# Threshold tuning can only ever lower the ODDS of a false-positive storm,
# never guarantee it can't happen again on some other image. Rebuilt on
# Impact Pack's SEGS pipeline instead (see _face_detail): detect -> keep
# ONLY the single largest region -> refine just that one. There's only ever
# one real face in these poses, so this is a hard structural cap, not
# another number to tune and hope. (The Krea2 sibling node's
# character_sheet_director.py had the same FaceDetailer-based vulnerability -
# ported the same SEGS rebuild there too.)
FACE_DETAIL_BBOX_KW = dict(threshold=0.75, dilation=10, crop_factor=2.0, drop_size=40, labels="all")
FACE_DETAIL_DETAILER_KW = dict(guide_size=1536.0, guide_size_for=False, max_size=1536.0, noise_mask_feather=10)
FACE_DETAIL_PASTE_KW = dict(feather=10, alpha=255)

_MODEL_CACHE = {"key": None}
_DETECTOR_CACHE = {"key": None}
_SESSIONS = {}


def _node(name):
    cls = comfy_nodes.NODE_CLASS_MAPPINGS.get(name)
    if cls is None:
        raise RuntimeError(
            f"[Man4TechCharacterSheetKlein] Required node '{name}' is not registered. "
            f"Check that its custom_nodes package is installed and loaded."
        )
    return cls()


def _tensor_hash(t):
    arr = t.detach().to("cpu", torch.float32).contiguous().numpy()
    return hashlib.sha1(arr.tobytes()).hexdigest()


def _signature(character_image, pose_reference_image, unet_name, clip_name, vae_name, kv_cache, steps, cfg,
               face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
               model_override=None, clip_override=None, vae_override=None):
    payload = [
        _tensor_hash(character_image),
        # [2026-09-20] Optional second reference, used purely as a structural
        # pose anchor (an extra ReferenceLatent hop) - deliberately never
        # mentioned in the prompt text itself, confirmed by Andy's own test
        # (same single-image prompt, second image added only to the graph,
        # pose held correctly). None<->tensor changes the sig just like
        # character_image does, so plugging/unplugging or swapping it
        # correctly invalidates the session.
        _tensor_hash(pose_reference_image) if pose_reference_image is not None else None,
        unet_name, clip_name, vae_name, kv_cache, int(steps), round(float(cfg), 4),
        bool(face_detail), face_detail_type, face_detail_sampler, face_detail_scheduler,
        round(float(face_detail_denoise), 4),
        # [2026-09-19] Boolean presence only, not id() - see the Krea2 node's
        # own _signature for the full story: keying a full session-wipe off
        # object identity across separate /prompt submissions caused a single
        # reroll click (nothing about the model config touched) to silently
        # invalidate and regenerate all 5 poses. Not repeating that mistake here.
        model_override is not None, clip_override is not None, vae_override is not None,
    ]
    return hashlib.sha1(json.dumps(payload).encode("utf-8")).hexdigest()


def _should_use_kv_cache(kv_cache, unet_name, using_override):
    """[2026-09-23] FluxKVCache caches reference-image key/value pairs across
    the 4 distilled steps - it's built specifically for the 9B-KV checkpoint
    (flux-2-klein-9b-kv-fp8) and produces broken/weird output on the plain 4B
    model, which has no KV-cache support (Andy confirmed this testing the
    Apache-licensed 4B model). "auto" detects the checkpoint from unet_name
    (every 9B-KV filename BFL ships has "kv" in it, the 4B one doesn't) -
    when a MODEL comes in via model_override instead there's no filename to
    check, so auto falls back to the original always-on behavior for that
    case (a generic loader has no reason to know about this pipeline-specific
    quirk); "on"/"off" force it either way regardless of source."""
    if kv_cache == "on":
        return True
    if kv_cache == "off":
        return False
    if using_override:
        return True
    return "kv" in (unet_name or "").lower()


def _get_models(unet_name, clip_name, vae_name, kv_cache, model_override=None, clip_override=None, vae_override=None):
    use_kv = _should_use_kv_cache(kv_cache, unet_name, model_override is not None)
    key = (
        unet_name, clip_name, vae_name, kv_cache,
        id(model_override) if model_override is not None else None,
        id(clip_override) if clip_override is not None else None,
        id(vae_override) if vae_override is not None else None,
    )
    if _MODEL_CACHE.get("key") == key:
        return _MODEL_CACHE

    print(f"[Man4TechCharacterSheetKlein] loading models: {key}", flush=True)
    model = model_override if model_override is not None else _node("UNETLoader").load_unet(unet_name, "default")[0]
    if use_kv:
        model = _node("FluxKVCache").execute(model=model)[0]
        print("[Man4TechCharacterSheetKlein] KV-cache: ON", flush=True)
    else:
        print("[Man4TechCharacterSheetKlein] KV-cache: OFF", flush=True)
    clip = clip_override if clip_override is not None else _node("CLIPLoader").load_clip(clip_name, "flux2", "default")[0]
    vae = vae_override if vae_override is not None else _node("VAELoader").load_vae(vae_name)[0]

    _MODEL_CACHE.clear()
    _MODEL_CACHE.update({"key": key, "model": model, "clip": clip, "vae": vae})
    return _MODEL_CACHE


def _get_detector(detector_type):
    model_name = FACE_DETAIL_DETECTOR_MODELS[detector_type]
    if _DETECTOR_CACHE.get("key") == model_name:
        return _DETECTOR_CACHE
    print(f"[Man4TechCharacterSheetKlein] loading detector: {model_name}", flush=True)
    bbox_detector, _segm_detector = _node("UltralyticsDetectorProvider").doit(model_name)
    _DETECTOR_CACHE.clear()
    _DETECTOR_CACHE.update({"key": model_name, "bbox_detector": bbox_detector})
    return _DETECTOR_CACHE


def _ensure_rgb(image):
    return image[..., :3] if image.shape[-1] > 3 else image


def _resize(image, width, height, method="lanczos"):
    samples = image.movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, method, "disabled")
    return samples.movedim(1, -1)


def _scaled_crop(pose_name, guide_image):
    """Identical mechanism to the Krea2 sibling node's own _scaled_crop -
    scales GUIDE_CROPS' rectangles (tuned to REFERENCE_GUIDE_SIZE) to whatever
    actual resolution the supplied pose_reference_image happens to be."""
    ref_w, ref_h = REFERENCE_GUIDE_SIZE
    actual_h, actual_w = guide_image.shape[1], guide_image.shape[2]
    sx, sy = actual_w / ref_w, actual_h / ref_h
    rect = GUIDE_CROPS[pose_name]
    x = int(round(rect["x"] * sx))
    y = int(round(rect["y"] * sy))
    w = int(round(rect["width"] * sx))
    h = int(round(rect["height"] * sy))
    x = max(0, min(x, actual_w - 1))
    y = max(0, min(y, actual_h - 1))
    w = max(1, min(w, actual_w - x))
    h = max(1, min(h, actual_h - y))
    return x, y, w, h


def _crop(image, x, y, w, h):
    return image[:, y:y + h, x:x + w, :]


def _face_detail(image, model, positive, negative, models, seed, cfg,
                  face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise):
    """[2026-09-17] SEGS pipeline, not a direct FaceDetailer call - see the
    FACE_DETAIL_* constants' comment for why. detect (BboxDetectorSEGS) ->
    keep only the single largest region (ImpactSEGSOrderedFilter,
    take_count=1) -> refine just that one (SEGSDetailer) -> composite back
    onto the full image (SEGSPaste). However many "faces" the detector
    reports - 1 or 300 - exactly one refine pass ever runs."""
    detector = _get_detector(face_detail_type)
    print(f"[Man4TechCharacterSheetKlein] >>> face-detail pass START (detect={face_detail_type}, "
          f"sampler={face_detail_sampler}, scheduler={face_detail_scheduler}, "
          f"denoise={face_detail_denoise}, steps={FACE_DETAIL_STEPS})", flush=True)
    raw_segs = _node("BboxDetectorSEGS").doit(
        bbox_detector=detector["bbox_detector"], image=image, **FACE_DETAIL_BBOX_KW,
    )[0]
    largest_seg, _ = _node("ImpactSEGSOrderedFilter").doit(
        segs=raw_segs, target="area(=w*h)", order=True, take_start=0, take_count=1,
    )
    found = len(largest_seg[1]) > 0
    if not found:
        print("[Man4TechCharacterSheetKlein] <<< face-detail pass END - NO region detected, image unchanged", flush=True)
        return image
    basic_pipe = _node("ToBasicPipe").doit(
        model=model, clip=models["clip"], vae=models["vae"], positive=positive, negative=negative,
    )[0]
    refined_segs, _ = _node("SEGSDetailer").doit(
        image=image, segs=largest_seg, seed=int(seed), steps=FACE_DETAIL_STEPS, cfg=float(cfg),
        sampler_name=face_detail_sampler, scheduler=face_detail_scheduler, denoise=float(face_detail_denoise),
        noise_mask=True, force_inpaint=True, basic_pipe=basic_pipe, refiner_ratio=0.2, batch_size=1, cycle=1,
        **FACE_DETAIL_DETAILER_KW,
    )
    result_image = _node("SEGSPaste").doit(image=image, segs=refined_segs, **FACE_DETAIL_PASTE_KW)[0]
    print("[Man4TechCharacterSheetKlein] <<< face-detail pass END - region found and refined", flush=True)
    return result_image


def _generate_pose(pose_idx, character_image, char_latent, pose_latent, models, seed, prompt,
                    steps, cfg,
                    face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise):
    pose_name = POSE_NAMES[pose_idx]
    target_w, target_h = TARGET_SIZE[pose_name]

    text_cond = _node("CLIPTextEncode").encode(models["clip"], prompt)[0]
    # [2026-09-19] Self-referencing pattern confirmed against the tested
    # single-image workflow: the SAME source latent (character reference) is
    # chained into both the positive and the zeroed-out negative path via
    # ReferenceLatent - there's no separate guide reference the way the Krea2
    # node has one, since this pipeline only ever has one image to work from.
    zeroed_cond = _node("ConditioningZeroOut").zero_out(text_cond)[0]
    negative = _node("ReferenceLatent").execute(conditioning=zeroed_cond, latent=char_latent)[0]
    positive = _node("ReferenceLatent").execute(conditioning=text_cond, latent=char_latent)[0]
    if pose_latent is not None:
        # [2026-09-20] Optional extra reference, chained AFTER the character
        # identity reference, purely as a structural pose anchor - the prompt
        # text never mentions it (confirmed by Andy's own test: same
        # single-image prompt, second image added only here in the graph,
        # and the pose held correctly). Only added to positive - negative is
        # a zeroed no-op at cfg=1 regardless, so chaining it there too would
        # just be extra work for no effect.
        positive = _node("ReferenceLatent").execute(conditioning=positive, latent=pose_latent)[0]

    target_latent = _node("EmptyFlux2LatentImage").execute(width=target_w, height=target_h, batch_size=1)[0]
    sampled = _node("KSampler").sample(
        model=models["model"], seed=int(seed), steps=int(steps), cfg=float(cfg),
        sampler_name="euler", scheduler="simple",
        positive=positive, negative=negative, latent_image=target_latent, denoise=1.0,
    )[0]
    decoded = _node("VAEDecode").decode(vae=models["vae"], samples=sampled)[0]

    if face_detail:
        decoded = _face_detail(
            decoded, models["model"], positive, negative, models, seed, cfg,
            face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
        )

    # Background cleanup here (not just at final assembly) so the per-pose
    # preview the user reviews/confirms already shows the finished white
    # background - same reasoning as the Krea2 node.
    clean_image, mask, _ = _node("RMBG").process_image(image=decoded, **RMBG_KW)
    return clean_image, mask


def _edit_pose(source_image, instruction, models, seed, target_w, target_h, steps, cfg,
               face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise):
    """[2026-09-20] Targeted follow-up edit on a pose's OWN already-generated
    pixels (e.g. "add high heel shoes") rather than a fresh regenerate from
    the original character/pose references. Klein is an edit model, so this
    is the same mechanism as the main pipeline - source_image chained in via
    ReferenceLatent, an EmptyFlux2LatentImage init (not img2img denoising
    from the source's own latent) - just with a different, one-off source
    and instruction. The pose's own stored base prompt is deliberately left
    untouched by this (see the "edit" action handling in run()), so a later
    normal re-roll still regenerates from the original pose description, not
    from whatever one-off edit instruction was typed here.
    """
    scaled_source = _node("ImageScaleToTotalPixels").execute(
        image=source_image, upscale_method="lanczos", megapixels=1.0, resolution_steps=1,
    )[0]
    source_latent = _node("VAEEncode").encode(vae=models["vae"], pixels=scaled_source)[0]

    text_cond = _node("CLIPTextEncode").encode(models["clip"], instruction)[0]
    zeroed_cond = _node("ConditioningZeroOut").zero_out(text_cond)[0]
    negative = _node("ReferenceLatent").execute(conditioning=zeroed_cond, latent=source_latent)[0]
    positive = _node("ReferenceLatent").execute(conditioning=text_cond, latent=source_latent)[0]

    target_latent = _node("EmptyFlux2LatentImage").execute(width=target_w, height=target_h, batch_size=1)[0]
    sampled = _node("KSampler").sample(
        model=models["model"], seed=int(seed), steps=int(steps), cfg=float(cfg),
        sampler_name="euler", scheduler="simple",
        positive=positive, negative=negative, latent_image=target_latent, denoise=1.0,
    )[0]
    decoded = _node("VAEDecode").decode(vae=models["vae"], samples=sampled)[0]

    if face_detail:
        decoded = _face_detail(
            decoded, models["model"], positive, negative, models, seed, cfg,
            face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
        )

    clean_image, mask, _ = _node("RMBG").process_image(image=decoded, **RMBG_KW)
    return clean_image, mask


def _load_preview_pixels(state, index):
    """Core file lookup shared by _restore_confirmed_preview (hard-fails if
    nothing's found, since a confirmed lock has to hold) and the "edit" action
    (best-effort - an edit request has nothing wrong with it if there simply
    isn't a prior generation to edit yet, e.g. right after a restart wiped
    the in-process session; the caller just skips gracefully). Returns
    pixels or None - never raises."""
    import os
    import numpy as np
    from PIL import Image
    previews = state.get("previews") or []
    item = previews[index] if index < len(previews) else None
    if not item or item.get("type") not in ("temp", "output"):
        return None
    base = folder_paths.get_temp_directory() if item["type"] == "temp" else folder_paths.get_output_directory()
    base = os.path.realpath(base)
    path = os.path.realpath(os.path.join(base, item.get("subfolder", ""), item.get("filename", "")))
    if os.path.commonpath([base, path]) != base or not os.path.isfile(path):
        return None
    with Image.open(path) as im:
        return torch.from_numpy(np.array(im.convert("RGB"), dtype=np.float32) / 255.0).unsqueeze(0)


def _restore_confirmed_preview(state, index, seed, prompt):
    """Identical mechanism to the Krea2 node's - see that file's docstring for
    the full rationale. A confirmed pose is a lock that has to survive both a
    settings change and a ComfyUI restart; this reloads its accepted pixels
    from the saved preview file instead of silently regenerating them."""
    pixels = _load_preview_pixels(state, index)
    if pixels is None:
        raise ValueError(
            f"Confirmed pose {index + 1}'s saved preview file is missing on disk "
            f"(temp previews can get cleaned up over time). Unconfirm it to regenerate."
        )
    _, mask, _ = _node("RMBG").process_image(image=pixels, **RMBG_KW)
    return {"seed": seed, "prompt": prompt, "image": pixels, "mask": mask}


def _assemble_final(sess, figure_type, output_scale=1.0):
    """[2026-09-17] output_scale (default 1.0 = the original 4096x2304)
    uniformly scales every panel's dimensions - portrait width/height and the
    body panels' width/height/figure_height/bottom_margin all move together,
    so proportions stay identical to the tuned defaults; only the overall
    size changes. Requested via a YouTube comment asking for a configurable
    final resolution.
    [2026-09-24] figure_type only swaps in FIGURE_TYPE_ALIGN_OVERRIDES'
    figure_height/bottom_margin before scaling (see _align_kw_for) - every
    other dimension, and MuseSheetAlignFigure itself, is unchanged."""
    scale = float(output_scale)
    portrait_size = (round(PORTRAIT_FINAL_SIZE[0] * scale), round(PORTRAIT_FINAL_SIZE[1] * scale))
    align_kw = _align_kw_for(figure_type, scale)
    panels = []
    for i in range(5):
        pose = sess["poses"][i]
        if i == 0:
            panel = _resize(pose["image"], *portrait_size)
        else:
            panel = _node("Man4TechSheetAlignFigure").align(image=pose["image"], mask=pose["mask"], **align_kw)[0]
        panels.append(panel)
    return torch.cat(panels, dim=2)


def _preview(image, persist=False):
    node = _node("SaveImage") if persist else _node("PreviewImage")
    kwargs = {"images": image}
    if persist:
        kwargs["filename_prefix"] = "Man4TechCharacterSheetKlein"
    result = node.save_images(**kwargs)
    return result["ui"]["images"][0]


class Man4TechCharacterSheetKlein:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "character_image": ("IMAGE", {"tooltip": "Reference photo of the character/outfit, used for every pose."}),
                "unet_name": (folder_paths.get_filename_list("diffusion_models"),),
                "clip_name": (folder_paths.get_filename_list("text_encoders"),),
                "vae_name": (folder_paths.get_filename_list("vae"),),
                "kv_cache": (["auto", "on", "off"], {"default": "auto", "tooltip": "FluxKVCache is a speed optimization that only works with the 9B-KV checkpoint (flux-2-klein-9b-kv-fp8) - it produces broken/weird output on the plain 4B model (no KV-cache support). 'auto' detects this from unet_name (looks for 'kv' in the filename); use 'off' if you're loading a 4B or non-KV checkpoint and 'auto' guesses wrong, or 'on'/'off' to force it either way."}),
                "steps": ("INT", {"default": 4, "min": 1, "max": 50, "tooltip": "FLUX.2 [klein] is step-distilled - 4 is the trained/tested value."}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1}),
                "face_detail": ("BOOLEAN", {"default": True, "tooltip": "Run a FaceDetailer pass on each pose to fix soft/plastic faces."}),
                "face_detail_type": (list(FACE_DETAIL_DETECTOR_MODELS.keys()), {"default": "face"}),
                "face_detail_sampler": (comfy.samplers.KSampler.SAMPLERS, {"default": "dpmpp_2m"}),
                "face_detail_scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "simple"}),
                "face_detail_denoise": ("FLOAT", {"default": 0.30, "min": 0.0, "max": 1.0, "step": 0.01}),
                "seed_mode": (["random", "fixed"], {"default": "random", "tooltip": "What starting seeds a fresh run gets AFTER a completed sheet resets (not the per-pose New seed button, which always picks a fresh random seed regardless). 'random' means running again with the same character photo produces different poses without needing a new image; 'fixed' always starts from the same seeds (41001-41005), so a re-run reproduces the same result."}),
                "state_json": ("STRING", {"multiline": True, "default": json.dumps(DEFAULT_STATE)}),
                # [2026-09-17] Appended AFTER state_json deliberately, never
                # inserted mid-list - widgets_values on an already-saved
                # workflow is positional, not by-name, so a mid-list insert
                # silently shifts every later widget's stored value onto the
                # wrong slot (this is exactly what just happened: an old
                # saved node's state_json value landed in this widget instead,
                # which then cascaded and scrambled unet_name/clip_name too).
                # New widgets always go at the true end from now on.
                "output_size": (list(OUTPUT_SIZE_PRESETS.keys()), {"default": "4096x2304 (Standard - default)", "tooltip": "Final assembled sheet size - pick the exact pixel dimensions you want. Every panel's proportions stay identical across presets, only the overall size changes. Only affects the final Build step, not the per-pose generation/preview resolution."}),
                # [2026-09-24] Appended AFTER output_size, same true-end rule
                # as output_size's own comment above - never insert mid-list.
                # Default "human" is deliberately the old, only-ever-tested
                # behavior - a workflow that never touches this widget
                # generates identical prompts to before this was added.
                # Picking anything else only changes each pose's
                # "presentation" clause (see FIGURE_TYPE_PRESENTATION) and,
                # for quadruped/floating, the final-assembly figure_height/
                # bottom_margin (see FIGURE_TYPE_ALIGN_OVERRIDES) - it only
                # applies to a prompt that's still unedited default text, so
                # it never clobbers a prompt you've hand-edited in the UI.
                "figure_type": (FIGURE_TYPE_CHOICES, {"default": "human", "tooltip": "What kind of figure the reference photo shows. Only changes each pose's neutral 'presentation' clause (e.g. 'four-legged' for quadruped) and, for quadruped/floating, the final sheet's alignment - never overwrites a prompt you've hand-edited. 'custom' disables the automatic default text entirely; write your own per-pose prompts in the node's UI."}),
            },
            # Same purely-additive override pattern as the Krea2 node - manual
            # unet_name/clip_name/vae_name widgets still work standalone; a
            # connected socket wins over its matching widget (see _get_models()).
            "optional": {
                "pose_reference_image": ("IMAGE", {"tooltip": "Optional second reference - a 5-panel mannequin pose guide sheet, same layout as the Krea2 sibling node's guide_image. Automatically cropped to the ONE matching panel per pose (never the whole sheet) and chained in as an extra ReferenceLatent, purely as a structural pose anchor - never mentioned in the prompt text. Confirmed to help hold a pose without needing to describe it."}),
                "model_override": ("MODEL", {"tooltip": "Optional. Overrides unet_name when connected."}),
                "clip_override": ("CLIP", {"tooltip": "Optional. Overrides clip_name when connected."}),
                "vae_override": ("VAE", {"tooltip": "Optional. Overrides vae_name when connected."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("character_sheet",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "Man4Tech/Character Sheet"
    DESCRIPTION = (
        "Generates a 5-pose FLUX.2 [klein] character sheet from a single character reference "
        "photo (no guide sheet required - poses are described directly in text). Optionally "
        "connect a second pose_reference_image as a purely structural pose anchor - it's never "
        "mentioned in the prompt text, just chained in as an extra reference. Confirm or "
        "re-roll each pose in the node's own UI; once all five are confirmed, click Build "
        "final sheet now to assemble the final 4096x2304 sheet."
    )

    def run(self, character_image, unet_name, clip_name, vae_name, kv_cache, steps, cfg,
            face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
            seed_mode, output_size, figure_type, state_json, unique_id, pose_reference_image=None, model_override=None, clip_override=None, vae_override=None):
        character_image = _ensure_rgb(character_image)
        if pose_reference_image is not None:
            pose_reference_image = _ensure_rgb(pose_reference_image)
        try:
            state = json.loads(state_json) if state_json else {}
        except (TypeError, ValueError):
            state = {}
        seeds = [int(s) for s in (state.get("seeds") or DEFAULT_SEEDS)]
        confirmed = list(state.get("confirmed") or [False] * 5)
        prompts = list(state.get("prompts") or DEFAULT_PROMPTS)
        if len(prompts) != 5:
            prompts = list(DEFAULT_PROMPTS)
        # [2026-09-24] Keep any UNEDITED preset prompt in sync with the
        # currently selected figure_type - e.g. switching human -> quadruped
        # updates the still-default prompts to the quadruped presentation
        # clause without a manual "Reset prompt" click per pose. A prompt
        # that no longer matches ANY preset's text (hand-edited in the UI,
        # or already carrying an edit_instruction) is left untouched.
        # "custom" opts out entirely - no preset exists to resync toward.
        if figure_type != "custom":
            figure_defaults = _build_pose_prompts(figure_type)
            for i, pose_name in enumerate(POSE_NAMES):
                if not confirmed[i] and _is_unedited_default(pose_name, prompts[i]):
                    prompts[i] = figure_defaults[pose_name]
        action = state.get("action")

        sig = _signature(character_image, pose_reference_image, unet_name, clip_name, vae_name, kv_cache, steps, cfg,
                          face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
                          model_override, clip_override, vae_override)
        sess = _SESSIONS.setdefault(str(unique_id), {})
        if sess.get("sig") != sig:
            retained = {i: p for i, p in sess.get("poses", {}).items() if i < len(confirmed) and confirmed[i]}
            sess.clear()
            sess["sig"] = sig
            sess["poses"] = retained
            if not (action and action.get("type") == "finalize"):
                action = None

        for i in range(5):
            if confirmed[i] and sess["poses"].get(i) is None:
                sess["poses"][i] = _restore_confirmed_preview(state, i, seeds[i], prompts[i])

        models = _get_models(unet_name, clip_name, vae_name, kv_cache, model_override, clip_override, vae_override)

        if "char_latent" not in sess:
            scaled_char = _node("ImageScaleToTotalPixels").execute(
                image=character_image, upscale_method="lanczos", megapixels=1.0, resolution_steps=1,
            )[0]
            sess["char_latent"] = _node("VAEEncode").encode(vae=models["vae"], pixels=scaled_char)[0]

        if "pose_latents" not in sess:
            # [2026-09-17] One cropped+encoded latent PER POSE now, not one
            # whole-sheet latent reused for all 5 - see GUIDE_CROPS' comment
            # for why. Computed once per session (same caching pattern as
            # char_latent), since pose_reference_image doesn't change
            # between actions within a session.
            if pose_reference_image is not None:
                sess["pose_latents"] = {}
                for idx, pose_name in enumerate(POSE_NAMES):
                    x, y, w, h = _scaled_crop(pose_name, pose_reference_image)
                    pose_crop = _crop(pose_reference_image, x, y, w, h)
                    if idx == 0:
                        pose_for_gen = pose_crop
                    else:
                        pose_for_gen = _node("Man4TechSheetAlignFigure").align(image=pose_crop, mask=None, **ALIGN_POSE_REF_KW)[0]
                    sess["pose_latents"][idx] = _node("VAEEncode").encode(vae=models["vae"], pixels=pose_for_gen)[0]
            else:
                sess["pose_latents"] = {idx: None for idx in range(5)}

        if action and action.get("type") == "reroll":
            i = int(action["pose"])
            if not confirmed[i]:
                seeds[i] = int(action["seed"])

        def run_edit(i, source_image, instruction):
            """Runs _edit_pose and stores the result, tagged with the
            instruction and the SOURCE it was applied to (edit_source_image) -
            that tag is what makes reroll_edit() below possible: re-running
            the same instruction with a new seed from the same starting
            point, instead of stacking onto whatever the previous edit
            produced. A plain fresh generation (the normal to_generate loop)
            always overwrites sess["poses"][i] with a brand-new dict that has
            no edit_instruction/edit_source_image keys at all, so an old edit
            tag can never survive into a genuinely fresh pose by accident.
            [2026-09-23] Deliberately does NOT fold the pose's base prompt in
            here (an earlier attempt at that did) - every POSE_PROMPTS entry
            contains "Same face, hairstyle, skin tone and outfit unchanged",
            which directly contradicts any edit instruction that changes
            clothing, and was confirmed to cause visible color bleed/merging
            (Andy: "the weird saturated color... merging things together").
            instruction alone is what actually gets sent as text
            conditioning."""
            pose_name = POSE_NAMES[i]
            target_w, target_h = TARGET_SIZE[pose_name]
            image, mask = _edit_pose(
                source_image, instruction, models, seeds[i], target_w, target_h, steps, cfg,
                face_detail, face_detail_type, face_detail_sampler, face_detail_scheduler, face_detail_denoise,
            )
            sess["poses"][i] = {
                "seed": seeds[i], "prompt": prompts[i], "image": image, "mask": mask,
                "edit_instruction": instruction, "edit_source_image": source_image,
            }
            comfy.model_management.soft_empty_cache()

        def apply_edit(i, instruction):
            """[2026-09-20] A one-off targeted edit of a pose's OWN already-
            generated pixels (e.g. "add high heel shoes") - not a fresh
            regenerate from the original character/pose references. Runs
            before to_generate is computed, so the edited result is already
            in sess["poses"] by the time that's built - since seed/prompt for
            this pose are left unchanged, the normal to_generate equality
            check naturally leaves it alone afterward, no special exclusion
            needed. Returns True if it actually ran. Shared by both the
            single-pose "edit" action and "edit_all".
            """
            instruction = (instruction or "").strip()
            if confirmed[i] or not instruction:
                return False
            if sess["poses"].get(i) is None:
                # [2026-09-20] The in-process session is wiped by a ComfyUI
                # restart (or a settings change on an unconfirmed pose - only
                # confirmed poses get retained across a sig change). Without
                # this, an edit request right after a restart found nothing
                # in sess["poses"], silently skipped, and fell straight
                # through to a normal fresh regeneration of every unconfirmed
                # pose - discarding the edit instruction with zero feedback.
                # Best-effort recovery from the pose's own last saved preview
                # file closes that gap; if even that comes up empty (file
                # genuinely gone), the edit is skipped below same as before.
                pixels = _load_preview_pixels(state, i)
                if pixels is not None:
                    sess["poses"][i] = {"seed": seeds[i], "prompt": prompts[i], "image": pixels, "mask": None}
            if sess["poses"].get(i) is None:
                return False
            pose = sess["poses"][i]
            prior_instruction = pose.get("edit_instruction")
            if prior_instruction:
                # [2026-09-22] Anchor off the SAME pristine pre-edit pixels
                # every time (edit_source_image), not this edit's own output -
                # this is a full denoise=1.0 regeneration guided to resemble
                # its source, not a touch-up, so repeatedly feeding it its own
                # prior output compounds like a photocopy of a photocopy
                # (visible as color drift after 3-4 stacked edits). Folding
                # the new instruction in alongside the old one keeps this a
                # SINGLE full regeneration off a clean source instead of a
                # chain of them.
                base_image = pose["edit_source_image"]
                combined_instruction = f"{prior_instruction}; {instruction}"
            else:
                base_image = pose["image"]
                combined_instruction = instruction
            pose_name = POSE_NAMES[i]
            print(f"[Man4TechCharacterSheetKlein] editing {pose_name}: {combined_instruction}", flush=True)
            run_edit(i, base_image, combined_instruction)
            return True

        def reroll_edit(i, new_seed):
            """[2026-09-20] "New seed" on a pose that currently has an active
            edit re-runs THAT SAME edit instruction with a new seed, sourced
            from edit_source_image (the pixels the edit was originally
            applied to) - not the base pose, and not the previous edit
            result. Without this, the only "New seed" available reverted to
            the un-edited pose every time, discarding the edit entirely -
            exactly the behavior Andy flagged. Returns True if it actually
            re-rolled; False means "nothing to reroll here" (no active edit
            tracked, most likely because a ComfyUI restart wiped the
            in-process session - edit_source_image has no disk-backed
            recovery the way confirmed-pose pixels do), and the caller falls
            back to a normal base reroll instead of silently doing nothing.
            """
            if confirmed[i]:
                return False
            pose = sess["poses"].get(i)
            instruction = pose.get("edit_instruction") if pose else None
            source_image = pose.get("edit_source_image") if pose else None
            if not instruction or source_image is None:
                return False
            seeds[i] = int(new_seed)
            pose_name = POSE_NAMES[i]
            print(f"[Man4TechCharacterSheetKlein] re-rolling edit on {pose_name}: {instruction} (seed={seeds[i]})", flush=True)
            run_edit(i, source_image, instruction)
            return True

        if action and action.get("type") == "edit":
            apply_edit(int(action["pose"]), action.get("instruction"))
        elif action and action.get("type") == "edit_all":
            # [2026-09-20] Same edit, applied to every UNCONFIRMED pose in one
            # go - confirmed/locked poses are silently skipped, same lock
            # semantics as everything else in this node.
            instruction = action.get("instruction")
            for i in range(5):
                apply_edit(i, instruction)
        elif action and action.get("type") == "reroll_edit":
            i = int(action["pose"])
            if not reroll_edit(i, action["seed"]):
                # Nothing to reroll (see reroll_edit's docstring) - fall back
                # to a normal base reroll rather than doing nothing at all.
                if not confirmed[i]:
                    seeds[i] = int(action["seed"])
        elif action and action.get("type") == "reset_edit":
            # [2026-09-17] Cancels an active edit and reverts to the plain
            # base pose - requested via a YouTube comment (edit an outfit,
            # decide you don't want it, but there was no way back to the
            # un-edited version short of retyping the base prompt). Just
            # dropping the cached pose is enough: seed/prompt haven't
            # changed, so to_generate's mismatch check won't catch it on its
            # own, but with no cached entry at all it unconditionally
            # regenerates at the CURRENT seed/prompt - a plain base
            # generation, since sess["poses"][i] never had edit_instruction/
            # edit_source_image keys to begin with once rebuilt this way.
            i = int(action["pose"])
            if not confirmed[i]:
                sess["poses"].pop(i, None)
        elif action and action.get("type") == "reset_prompt":
            # [2026-09-17] "Reset prompt to default" - Andy: "even if you
            # change it and mess it all up, you can hit default prompt and
            # it will do that." Reverts this pose's prompt back to its
            # built-in POSE_PROMPTS text and lets it regenerate with it -
            # the normal to_generate mismatch check (prompt changed) picks
            # this up on its own, no special regeneration path needed.
            i = int(action["pose"])
            if not confirmed[i]:
                prompts[i] = _build_pose_prompts(figure_type)[POSE_NAMES[i]]
        elif not action and seed_mode == "random":
            # [2026-09-23] A bare "hit Run" (no button clicked - action is
            # None) used to just replay whatever was already cached, since
            # nothing about seeds/prompts/settings had changed - correct for
            # avoiding wasted GPU work, but not what Andy actually wants:
            # a normal ComfyUI workflow with a random seed regenerates every
            # single time you queue it, full stop, no extra clicks required.
            # This reroll-everything-unconfirmed-on-a-bare-run is what makes
            # that true here too, while "fixed" mode (or a confirmed pose)
            # keeps the old reproducible/locked behavior untouched. Any
            # EXPLICIT action (reroll/edit/reroll_edit/finalize) already sets
            # its own seed(s) above and is excluded by the `not action` check,
            # so this can't double-randomize a pose the user just picked.
            for i in range(5):
                if not confirmed[i]:
                    seeds[i] = random.randint(0, 2 ** 31 - 1)

        to_generate = [i for i in range(5)
                       if not confirmed[i] and (
                           sess["poses"].get(i) is None
                           or sess["poses"][i]["seed"] != seeds[i]
                           or sess["poses"][i]["prompt"] != prompts[i]
                       )]
        for i in to_generate:
            print(f"[Man4TechCharacterSheetKlein] generating {POSE_NAMES[i]} (seed={seeds[i]})", flush=True)
            image, mask = _generate_pose(i, character_image, sess["char_latent"], sess["pose_latents"][i], models,
                                          seeds[i], prompts[i], steps, cfg,
                                          face_detail, face_detail_type, face_detail_sampler,
                                          face_detail_scheduler, face_detail_denoise)
            sess["poses"][i] = {"seed": seeds[i], "prompt": prompts[i], "image": image, "mask": mask}
            comfy.model_management.soft_empty_cache()

        want_finalize = bool(action) and action.get("type") == "finalize"
        final_image = None
        if want_finalize:
            if not all(confirmed):
                status = "not_all_confirmed"
            else:
                print("[Man4TechCharacterSheetKlein] assembling final sheet", flush=True)
                final_image = _assemble_final(sess, figure_type, OUTPUT_SIZE_PRESETS.get(output_size, 1.0))
                status = "finalized"
        else:
            status = "generating" if to_generate else "ready"

        pose_previews = [_preview(sess["poses"][i]["image"], persist=confirmed[i]) for i in range(5)]
        ui = {
            "pose_previews": pose_previews,
            "pose_labels": POSE_LABELS,
            "seeds": seeds,
            "prompts": prompts,
            "confirmed": confirmed,
            "status": [status],
        }
        if final_image is not None:
            sess.clear()
            # [2026-09-20] seed_mode governs ONLY this post-finalize reset -
            # per-pose "New seed" always picks a fresh random seed regardless
            # of this setting (see the reroll action, unchanged). Without a
            # "random" option here, running the SAME character photo again
            # after a completed sheet would always restart from the exact
            # same DEFAULT_SEEDS and produce byte-identical poses - the only
            # way to get a different result would be feeding in a different
            # image, which isn't what "just click Run again" should require.
            if seed_mode == "random":
                ui["seeds"] = [random.randint(0, 2 ** 31 - 1) for _ in range(5)]
            else:
                ui["seeds"] = list(DEFAULT_SEEDS)
            ui["confirmed"] = [False] * 5
            ui["reset"] = [True]

        result = (final_image,) if final_image is not None else (ExecutionBlocker(None),)
        return {"ui": ui, "result": result}


NODE_CLASS_MAPPINGS = {"Man4TechCharacterSheetKlein": Man4TechCharacterSheetKlein}
NODE_DISPLAY_NAME_MAPPINGS = {"Man4TechCharacterSheetKlein": "Man4Tech Character Sheet (Klein)"}
