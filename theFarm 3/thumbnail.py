"""Farm 2.2 ComfyUI thumbnail workflow, separate from the in-video title header."""
import argparse
import io
import json
import os
from pathlib import Path
import secrets
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
import uuid
import config as settings
from brain import ThumbnailPlan, atomic_json as dump, fingerprint, read_json


def thumbnail_workflow(path, prompt, prefix):
    """Fill the positive prompt in the user's API workflow; preserve model/settings."""
    workflow = json.loads(path.read_text())
    samplers = [n for n in workflow.values() if n.get("class_type") == "KSampler"]
    if not samplers:
        raise ValueError("Thumbnail workflow must contain a KSampler node.")
    for sampler in samplers:
        positive = workflow[sampler["inputs"]["positive"][0]]
        if positive.get("class_type") != "CLIPTextEncode":
            raise ValueError(
                "Expected a CLIPTextEncode positive prompt in basicImage.json."
            )
        positive["inputs"]["text"] = prompt
        sampler["inputs"]["seed"] = secrets.randbelow(2**53)
    saves = [
        key for key, node in workflow.items() if node.get("class_type") == "SaveImage"
    ]
    if not saves:
        raise ValueError("Thumbnail workflow needs a SaveImage output.")
    for node in workflow.values():
        if node.get("class_type") in {"EmptyLatentImage", "EmptySD3LatentImage"}:
            node["inputs"].update(width=settings.THUMBNAIL_WIDTH, height=settings.THUMBNAIL_HEIGHT)
    for key in saves:
        workflow[key]["inputs"]["filename_prefix"] = prefix
    return (workflow, saves)


def _generate_thumbnail(plan, workflow_path, server, folder, output, timeout=900):
    """Queue once, poll only this job, then save its image beside the finished video."""
    from PIL import Image

    target = output.with_name(output.stem + "_thumbnail.png")
    if target.is_file():
        with Image.open(target) as existing:
            existing.verify()
        return target
    server = server.rstrip("/")
    if urllib.parse.urlsplit(server).scheme not in {"http", "https"}:
        raise ValueError("ComfyUI URL must start with http:// or https://.")
    if not plan.thumbnail_prompt.strip():
        raise ValueError(
            "This saved plan has no thumbnail_prompt; generate a new plan or add one."
        )
    workflow, saves = thumbnail_workflow(
        workflow_path, plan.thumbnail_prompt, "aiEditor_" + folder.name
    )
    dump(folder / "thumbnail_workflow.json", workflow)
    (folder / "thumbnail_prompt.txt").write_text(plan.thumbnail_prompt)
    deadline = time.monotonic() + timeout

    def request(route, data=None, binary=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "ComfyUI thumbnail timed out; the queued job may still finish on the server."
            )
        payload = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            server + route,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=min(30, remaining)) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"ComfyUI HTTP {error.code}: {detail}") from error
        return raw if binary else json.loads(raw)

    status_path = folder / "thumbnail_status.json"
    status = json.loads(status_path.read_text()) if status_path.is_file() else {}
    if status.get("status") == "submitting":
        raise RuntimeError("Previous thumbnail submission is uncertain; inspect ComfyUI before resubmitting.")
    if status.get("prompt_id") and status.get("status") in ("queued", "failed"):
        job = status["prompt_id"]
        print("Resuming existing ComfyUI thumbnail job...", flush=True)
    else:
        status = {"status": "submitting"}
        dump(status_path, status)
        try:
            queued = request(
                "/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())}
            )
        except urllib.error.URLError as error:
            if isinstance(error.reason, ConnectionRefusedError):
                # A refused connection never reached the server, so retrying is safe.
                status.update(status="not_submitted")
                dump(status_path, status)
            raise
        if queued.get("node_errors") or not queued.get("prompt_id"):
            raise RuntimeError(
                "ComfyUI rejected the workflow: " + json.dumps(queued)[:2000]
            )
        job = queued["prompt_id"]
        status.update(status="queued", prompt_id=job)
        dump(status_path, status)
    print("Generating thumbnail with ComfyUI...", flush=True)
    while time.monotonic() < deadline:
        history = request("/history/" + urllib.parse.quote(str(job), safe="")).get(job)
        if history:
            if history.get("status", {}).get("status_str") == "error":
                dump(folder / "thumbnail_history.json", history)
                raise RuntimeError(
                    "ComfyUI generation failed; see thumbnail_history.json."
                )
            images = [
                im
                for key in saves
                for im in history.get("outputs", {}).get(key, {}).get("images", [])
            ]
            if images:
                im = images[0]
                query = urllib.parse.urlencode(
                    {k: im.get(k, "") for k in ("filename", "subfolder", "type")}
                )
                raw = request("/view?" + query, binary=True)
                target = output.with_name(output.stem + "_thumbnail.png")
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    temporary_image = target.with_suffix(".partial.png")
                    image.convert("RGB").save(temporary_image, format="PNG")
                    os.replace(temporary_image, target)
                status.update(status="complete", output=str(target))
                dump(folder / "thumbnail_status.json", status)
                return target
            if history.get("status", {}).get("completed"):
                raise RuntimeError(
                    "ComfyUI completed without an image in the SaveImage output."
                )
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise TimeoutError(
        "ComfyUI thumbnail timed out; the queued job may still finish on the server."
    )


def generate_thumbnail(plan, folder):
    """Create/resume the original ComfyUI thumbnail, preserving its workflow settings."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    plan = ThumbnailPlan.model_validate(plan.model_dump() if hasattr(plan, "model_dump") else plan)
    workflow = Path(settings.THUMBNAIL_WORKFLOW)
    if not workflow.is_file():
        raise FileNotFoundError(f"Original ComfyUI API workflow missing: {workflow}")
    key = fingerprint({"plan": plan.model_dump(), "workflow": workflow.read_text(),
                       "width": settings.THUMBNAIL_WIDTH, "height": settings.THUMBNAIL_HEIGHT,
                       "server": settings.COMFYUI_URL})
    state = read_json(folder / "thumbnail_binding.json")
    if state and state.get("key") != key:
        raise ValueError("Thumbnail plan/workflow changed; review or archive the previous thumbnail job before replacing it")
    dump(folder / "thumbnail_binding.json", {"key": key})
    return _generate_thumbnail(SimpleNamespace(thumbnail_prompt=plan.image_generation_prompt), workflow,
                               settings.COMFYUI_URL, folder, folder / "final.mp4", settings.THUMBNAIL_TIMEOUT)


def main():
    parser = argparse.ArgumentParser(description="Generate/resume the old ComfyUI thumbnail for a completed clip")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    plan = read_json(args.folder / "thumbnail_plan.json")
    if not plan:
        parser.error("This clip has no validated thumbnail_plan.json")
    print(generate_thumbnail(plan, args.folder))


if __name__ == "__main__":
    main()
