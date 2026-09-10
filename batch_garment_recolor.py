#!/usr/bin/env python3
"""
Batch Garment Recolor - Process multiple garment folders in parallel.

This script wraps garment_recolor.py to process multiple garment folders concurrently,
with a configurable parallelism level (default: 3, max: 5).

Each parallel worker runs a full independent workflow (auth, upload, job, download)
so there is no shared state between threads. Each subfolder is one garment set and
becomes one job, recolored consistently across its images.

The recolor target (--reference-image / --reference-text / --instruction and
--garment-label) is shared across every folder in the batch.

Usage:
    # Process all subfolders in a directory
    python batch_garment_recolor.py \
        --input-dir garments/ \
        --token YOUR_API_TOKEN \
        --instruction "make it forest green" \
        --output-dir results/

    # Process specific folders toward a colour reference image
    python batch_garment_recolor.py \
        --input-folders garments/TEE1 garments/TEE2 garments/TEE3 \
        --token YOUR_API_TOKEN \
        --reference-image swatches/forest-green.jpg \
        --garment-label "t-shirt" \
        --output-dir results/ \
        --parallel 5
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from garment_recolor import GarmentRecolor


def process_single_set(base_url, token, input_folder, output_folder,
                       garment_label, reference_image, reference_text, instruction,
                       num_variations, model, processing_size, post_process,
                       use_anchor, add_ai_watermark):
    """Process a single garment folder. Runs in its own thread with its own GarmentRecolor instance."""
    start = time.time()

    processor = GarmentRecolor(
        base_url=base_url,
        token=token,
        input_folder=str(input_folder),
        output_folder=str(output_folder),
        garment_label=garment_label,
        reference_image=reference_image,
        reference_text=reference_text,
        instruction=instruction,
        num_variations=num_variations,
        model=model,
        processing_size=processing_size,
        post_process=post_process,
        use_anchor=use_anchor,
        add_ai_watermark=add_ai_watermark,
    )

    success = processor.run()
    elapsed = time.time() - start

    return {
        "folder": input_folder.name,
        "success": success,
        "processing_time": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Batch Garment Recolor - Process multiple garment folders in parallel"
    )

    # Input: either --input-dir (all subfolders) or --input-folders (specific paths)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input-dir",
        type=str,
        help="Directory containing garment subfolders (each subfolder is processed as a separate job)",
    )
    input_group.add_argument(
        "--input-folders",
        type=str,
        nargs="+",
        help="Specific garment folder paths to process",
    )

    parser.add_argument(
        "--token", type=str, required=True, help="API token from https://app.on-model.com/profile?tab=tokens"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Base output directory — results saved to <output-dir>/<folder-name>/ (default: output)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Number of parallel workers (default: 3, max: 5)",
    )

    # Recolor target (at least one required; shared across all folders)
    target_group = parser.add_argument_group("recolor target (at least one required)")
    target_group.add_argument(
        "--garment-label", type=str, default=None,
        help="Which garment to recolor, e.g. 't-shirt'. Auto-detected when omitted.",
    )
    target_group.add_argument(
        "--reference-image", type=str, default=None,
        help="Path to a colour/pattern reference image to recolor toward.",
    )
    target_group.add_argument(
        "--reference-text", type=str, default=None,
        help="Colour or pattern as text, e.g. '#2E5A3B' or 'forest green'.",
    )
    target_group.add_argument(
        "--instruction", type=str, default=None,
        help="Free-form recolor instruction, e.g. 'make it forest green'.",
    )

    # Output options
    output_group = parser.add_argument_group("output options")
    output_group.add_argument(
        "--num-variations", type=int, default=1,
        help="Number of output variations per input image (1-8, default: 1)",
    )
    output_group.add_argument(
        "--processing-size", type=int, default=2048, choices=[1024, 2048, 4096],
        help="Output resolution in pixels: 1024 | 2048 | 4096 (default: 2048). "
             "Drives credit cost: 3 / 5 / 10 credits per output.",
    )

    # Generation options
    generation_group = parser.add_argument_group("generation options")
    generation_group.add_argument(
        "--model",
        choices=["auto", "nano_banana_2", "nano_banana_pro", "seedream", "seedream_5_pro",
                 "gpt_image", "gpt_image_2_5"],
        default="auto",
        help="Generation engine: auto | nano_banana_2 | nano_banana_pro | seedream | seedream_5_pro | gpt_image | gpt_image_2_5. "
             "'auto' (default) uses the default engine with a safety fallback.",
    )
    generation_group.add_argument(
        "--no-use-anchor",
        dest="use_anchor",
        action="store_false",
        default=True,
        help="Disable cross-output consistency. On by default.",
    )
    generation_group.add_argument(
        "--no-post-process",
        dest="post_process",
        action="store_false",
        default=True,
        help="Disable the finishing pass. On by default.",
    )
    generation_group.add_argument(
        "--add-ai-watermark",
        action="store_true",
        default=False,
        help="Bake an 'AI-generated' disclosure mark into each output (irreversible).",
    )

    args = parser.parse_args()

    # Enforce the API rule client-side: at least one recolor target is required.
    if not args.reference_image and not args.reference_text and not args.instruction:
        parser.error(
            "Provide at least one recolor target: --reference-image, --reference-text, or --instruction"
        )

    # Cap parallelism at 5 to respect API rate limits
    parallel = max(1, min(args.parallel, 5))

    # Collect input folders
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.exists():
            print(f"Input directory not found: {input_dir}")
            exit(1)
        folders = sorted([f for f in input_dir.iterdir() if f.is_dir()])
    else:
        folders = [Path(f) for f in args.input_folders]
        missing = [f for f in folders if not f.exists()]
        if missing:
            for f in missing:
                print(f"Folder not found: {f}")
            exit(1)

    if not folders:
        print("No folders to process")
        exit(1)

    output_dir = Path(args.output_dir)

    print("=" * 70)
    print("Batch Garment Recolor")
    print("=" * 70)
    print(f"  Folders to process: {len(folders)}")
    print(f"  Parallel workers:   {parallel}")
    print(f"  Output directory:   {output_dir}")
    print(f"  Model:              {args.model}")
    print(f"  Garment label:      {args.garment_label or 'auto-detect'}")
    print(f"  Anchor:             {'on' if args.use_anchor else 'off'}")
    print(f"  Processing size:    {args.processing_size}")
    print(f"  API base URL:       {args.base_url}")
    print("=" * 70)

    for i, folder in enumerate(folders, 1):
        print(f"  {i}. {folder.name}")
    print()

    start_time = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        future_to_folder = {}

        for folder in folders:
            per_folder_output = output_dir / folder.name
            future = executor.submit(
                process_single_set,
                args.base_url,
                args.token,
                folder,
                per_folder_output,
                args.garment_label,
                args.reference_image,
                args.reference_text,
                args.instruction,
                args.num_variations,
                args.model,
                args.processing_size,
                args.post_process,
                args.use_anchor,
                args.add_ai_watermark,
            )
            future_to_folder[future] = folder.name

        for future in as_completed(future_to_folder):
            folder_name = future_to_folder[future]
            try:
                result = future.result()
                results.append(result)
                status = "OK" if result["success"] else "FAILED"
                print(
                    f"\n[{len(results)}/{len(folders)}] {folder_name}: {status}"
                    f" ({result['processing_time']}s)"
                )
            except Exception as e:
                results.append({"folder": folder_name, "success": False, "error": str(e)})
                print(f"\n[{len(results)}/{len(folders)}] {folder_name}: ERROR - {e}")

    # Summary
    elapsed = time.time() - start_time
    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful

    print(f"\n{'=' * 70}")
    print("Summary")
    print(f"{'=' * 70}")
    print(f"  Total:      {len(results)}")
    print(f"  Successful: {successful}")
    print(f"  Failed:     {failed}")
    print(f"  Time:       {elapsed:.1f}s ({elapsed / 60:.1f} minutes)")

    if failed > 0:
        print(f"\n  Failed folders:")
        for r in results:
            if not r["success"]:
                error = r.get("error", "see console output above")
                print(f"    - {r['folder']}: {error}")

    print(f"{'=' * 70}")

    # Save batch summary
    summary_file = output_dir / f"batch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_file, "w") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "configuration": {
                    "parallel": parallel,
                    "model": args.model,
                    "garment_label": args.garment_label,
                    "reference_image": args.reference_image,
                    "reference_text": args.reference_text,
                    "instruction": args.instruction,
                    "num_variations": args.num_variations,
                    "processing_size": args.processing_size,
                    "use_anchor": args.use_anchor,
                    "post_process": args.post_process,
                    "add_ai_watermark": args.add_ai_watermark,
                    "base_url": args.base_url,
                },
                "total_folders": len(results),
                "successful": successful,
                "failed": failed,
                "total_time_seconds": round(elapsed, 1),
                "results": results,
            },
            f,
            indent=2,
        )
    print(f"Batch summary saved to {summary_file}")

    if failed > 0:
        exit(1)


if __name__ == "__main__":
    main()
