#!/usr/bin/env python3
"""
Minimal script to recolor a garment across a folder of product images with garment-recolor.

This script performs the basic workflow:
1. Create a project
2. Upload product images (and, optionally, a colour/pattern reference image)
3. Create a garment-recolor job
4. Monitor job progress
5. Download results

Authentication uses an API token generated at https://app.on-model.com/profile?tab=tokens

Like create-packshot, garment-recolor does NOT require an identity: it recolors the
garment in your existing product images and keeps everything else untouched. The recolor
is kept consistent across the whole set.
"""

import argparse
import http.client
import json
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class GarmentRecolor:
    def __init__(self, base_url, token, input_folder, output_folder="output",
                 garment_label=None, reference_image=None, reference_text=None,
                 instruction=None, num_variations=1, model="auto",
                 processing_size=2048, post_process=True, use_anchor=True,
                 add_ai_watermark=False):
        self.base_url = base_url.rstrip("/")
        self.input_folder = Path(input_folder)
        self.output_folder = Path(output_folder)

        # Recolor target (at least one of reference_image / reference_text / instruction)
        self.garment_label = garment_label
        self.reference_image_path = Path(reference_image) if reference_image else None
        self.reference_text = reference_text
        self.instruction = instruction

        # Output + generation options
        self.num_variations = num_variations
        self.model = model
        self.processing_size = processing_size
        self.post_process = post_process
        self.use_anchor = use_anchor
        self.add_ai_watermark = add_ai_watermark

        self.access_token = token
        self.project_id = None
        self.project_name = None
        self.reference_image_id = None

    def get_auth_headers(self):
        """Get headers with Bearer token."""
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request_with_retry(self, method, url, max_retries=5, initial_delay=1.0, max_delay=60.0, **kwargs):
        """Make an authenticated request with retry on rate limiting (429).

        Args:
            method: HTTP method ('get', 'post', etc.)
            url: Full URL to request
            max_retries: Maximum retry attempts for 429 responses (default: 5)
            initial_delay: Initial backoff delay in seconds (default: 1.0)
            max_delay: Maximum delay between retries (default: 60.0)
            **kwargs: Additional arguments passed to requests (json, params, timeout, etc.)

        Returns:
            Response object
        """
        delay = initial_delay
        request_func = getattr(requests, method.lower())

        for attempt in range(max_retries + 1):
            headers = {**kwargs.pop("headers", {}), **self.get_auth_headers()}
            response = request_func(url, headers=headers, **kwargs)

            # Handle 401 - token expired or invalid
            if response.status_code == 401:
                print("Token expired or invalid. Generate a new one at https://app.on-model.com/profile?tab=tokens")
                return response

            # Not rate limited - return immediately
            if response.status_code != 429:
                return response

            # Rate limited (429) - retry with exponential backoff + jitter
            if attempt < max_retries:
                jitter = delay * 0.2 * (2 * random.random() - 1)
                wait_time = min(delay + jitter, max_delay)
                print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                delay = min(delay * 2, max_delay)
            else:
                print(f"Rate limited (429). Max retries ({max_retries}) exceeded.")

        return response

    def create_project(self, project_name):
        """Create a project on the API server."""
        print(f"Creating project '{project_name}'...")

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/project",
                json={"project_name": project_name}
            )

            if response.status_code in [200, 201]:
                data = response.json()
                self.project_id = data["project_id"]
                self.project_name = data["project_name"]
                print(f"Project created: {self.project_id}")
                return True
            elif response.status_code == 409:
                # Project already exists, get its ID from the response
                print(f"Project '{project_name}' already exists")
                data = response.json()
                if "project_id" in data:
                    self.project_id = data["project_id"]
                    self.project_name = project_name
                    print(f"Using existing project: {self.project_id}")
                    return True
                # Fallback: list projects to find the matching one
                return self._find_project_by_name(project_name)
            else:
                print(f"Failed to create project: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"Error creating project: {e}")
            return False

    def _find_project_by_name(self, project_name):
        """Find an existing project by name via the list endpoint."""
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/project",
                params={"per_page": 100}
            )
            if response.status_code == 200:
                data = response.json()
                for project in data.get("projects", []):
                    if project.get("project_text") == project_name:
                        self.project_id = project.get("project_key", project.get("project_id"))
                        self.project_name = project_name
                        print(f"Found existing project: {self.project_id}")
                        return True
            print(f"Could not find project '{project_name}'")
            return False
        except Exception as e:
            print(f"Error listing projects: {e}")
            return False

    def get_upload_url(self, filename):
        """Get a pre-signed upload URL for an image."""
        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/upload",
                json={"filename": filename}
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get upload URL: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error getting upload URL: {e}")
            return None

    def upload_image(self, upload_url, file_path, content_type):
        """Upload an image to S3 using the pre-signed URL."""
        try:
            with open(file_path, "rb") as f:
                image_data = f.read()

            parsed = urlparse(upload_url)

            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
            else:
                conn = http.client.HTTPConnection(parsed.netloc, timeout=120)

            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"

            headers = {"Content-Type": content_type}
            conn.request("PUT", path, body=image_data, headers=headers)
            response = conn.getresponse()
            response.read()  # Read response to complete request
            conn.close()

            return response.status in [200, 201]
        except Exception as e:
            print(f"Error uploading image: {e}")
            return False

    def _upload_one(self, file_path):
        """Upload a single image file and return its file_id (or None on failure)."""
        upload_info = self.get_upload_url(file_path.name)
        if not upload_info:
            print(f"Failed to get upload URL for {file_path.name}")
            return None

        upload_url = upload_info["upload_url"]
        # Fix URL scheme if needed
        if self.base_url.startswith("https://") and upload_url.startswith("http://"):
            upload_url = upload_url.replace("http://", "https://", 1)

        if self.upload_image(upload_url, file_path, upload_info["content_type"]):
            print(f"Uploaded: {file_path.name} -> {upload_info['file_id']}")
            return upload_info["file_id"]

        print(f"Failed to upload: {file_path.name}")
        return None

    def upload_input_images(self):
        """Upload all product images from the input folder."""
        if not self.input_folder.exists():
            print(f"Input folder not found: {self.input_folder}")
            print(f"Checked path: {self.input_folder.absolute()}")
            return []

        # Find all image files (skip dotfiles and hidden)
        image_extensions = [".jpg", ".jpeg", ".png"]
        image_files = [
            f for f in sorted(self.input_folder.iterdir())
            if f.is_file()
            and f.suffix.lower() in image_extensions
            and not f.name.startswith(".")
        ]

        if not image_files:
            print(f"No images found in {self.input_folder}")
            return []

        if len(image_files) > 10:
            print(f"Warning: found {len(image_files)} images; garment-recolor accepts max 10. Truncating to first 10.")
            image_files = image_files[:10]

        print(f"Found {len(image_files)} product images to upload")

        # Create project if not already created
        if not self.project_name:
            project_name = self.input_folder.name
            if not self.create_project(project_name):
                return []

        file_ids = []
        for image_file in image_files:
            print(f"Uploading {image_file.name}...")
            file_id = self._upload_one(image_file)
            if file_id:
                file_ids.append(file_id)

        print(f"Successfully uploaded {len(file_ids)}/{len(image_files)} product images")
        return file_ids

    def upload_reference_image(self):
        """Upload the optional colour/pattern reference image; return its file_id."""
        ref = self.reference_image_path
        if not ref.exists():
            print(f"Reference image not found: {ref}")
            return None
        print(f"Uploading colour reference {ref.name}...")
        return self._upload_one(ref)

    def create_job(self, file_ids):
        """Create a garment-recolor job.

        Every input image is recolored, producing len(images) x num_variations outputs.
        The recolor target is a reference image, a colour/pattern text, an instruction,
        or any combination (at least one is required).
        """
        print("Creating garment-recolor job...")

        payload = {
            "project_id": self.project_id,
            "images": file_ids,
            "num_variations": self.num_variations,
            "model": self.model,
            "processing_size": self.processing_size,
            "post_process": self.post_process,
            "use_anchor": self.use_anchor,
            "add_ai_watermark": self.add_ai_watermark,
        }
        if self.garment_label:
            payload["garment_label"] = self.garment_label
        if self.reference_image_id:
            payload["reference_image"] = self.reference_image_id
        if self.reference_text:
            payload["reference_text"] = self.reference_text
        if self.instruction:
            payload["instruction"] = self.instruction

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/garment-recolor",
                json=payload
            )

            if response.status_code == 202:
                data = response.json()
                job_id = data["job_id"]
                total_outputs = data.get("total_outputs", len(file_ids) * self.num_variations)
                print(f"Job created: {job_id} ({total_outputs} outputs)")
                return job_id
            else:
                print(f"Failed to create job: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error creating job: {e}")
            return None

    def wait_for_job(self, job_id, max_wait_time=1200, check_interval=5):
        """Wait for job to complete."""
        print("Waiting for job to complete...")

        start_time = time.time()
        max_retries_404 = 10
        retry_count = 0

        time.sleep(5)  # Initial delay

        while True:
            if time.time() - start_time > max_wait_time:
                print(f"Timeout: Job took longer than {max_wait_time} seconds")
                return None

            try:
                response = self._request_with_retry(
                    "get",
                    f"{self.base_url}/jobs/{job_id}/status"
                )

                if response.status_code == 404:
                    retry_count += 1
                    if retry_count <= max_retries_404:
                        print(f"Job not found yet (attempt {retry_count}/{max_retries_404}), waiting...")
                        time.sleep(30)
                        continue
                    else:
                        print(f"Job {job_id} not found after {max_retries_404} retries")
                        return None

                if response.status_code != 200:
                    print(f"Failed to get status: {response.status_code}")
                    print(f"Response: {response.text}")
                    return None

                retry_count = 0
                status_data = response.json()
                status = status_data["status"]
                progress = status_data.get("progress", 0)

                print(f"Progress: {progress:.1f}% - Status: {status}")

                if status in ["completed", "failed", "aborted"]:
                    print(f"Job finished with status: {status}")
                    return status

                time.sleep(check_interval)
            except Exception as e:
                print(f"Error checking status: {e}")
                time.sleep(check_interval)

    def download_results(self, job_id):
        """Download job results."""
        print("Downloading results...")

        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/jobs/{job_id}/results"
            )

            if response.status_code != 200:
                print(f"Failed to get results: {response.status_code}")
                print(f"Response: {response.text}")
                return False

            results_data = response.json()

            # Create output folder
            self.output_folder.mkdir(parents=True, exist_ok=True)

            # Download images
            if "results" in results_data:
                for result in results_data["results"]:
                    if result.get("status") == "completed":
                        output_url = None
                        if result.get("output") and isinstance(result["output"], dict):
                            output_url = result["output"].get("full_size")

                        if output_url:
                            try:
                                img_response = requests.get(output_url, timeout=30)
                                img_response.raise_for_status()

                                image_index = result.get("image_index", 0)
                                group_index = result.get("group_index", 0)
                                version = result.get("version", 0)
                                fmt = result.get("output_format", "jpg")
                                filename = f"output_{image_index}_{group_index}_v{version}.{fmt}"

                                output_path = self.output_folder / filename
                                with open(output_path, "wb") as f:
                                    f.write(img_response.content)

                                model_used = result.get("model_used")
                                if model_used:
                                    print(f"Downloaded: {filename} (model: {model_used})")
                                else:
                                    print(f"Downloaded: {filename}")
                            except Exception as e:
                                print(f"Failed to download image {result.get('image_index')}: {e}")

            # Save metadata
            metadata_path = self.output_folder / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(results_data, f, indent=2)

            print(f"Results saved to {self.output_folder}")
            return True
        except Exception as e:
            print(f"Error downloading results: {e}")
            return False

    def run(self):
        """Run the complete workflow."""
        print("=" * 70)
        print("Garment Recolor")
        print("=" * 70)

        # Step 1: Upload product images (creates the project)
        file_ids = self.upload_input_images()
        if not file_ids:
            print("No images uploaded")
            return False

        if not self.project_id:
            print("No project ID available")
            return False

        # Step 2: Upload the optional colour/pattern reference image
        if self.reference_image_path:
            self.reference_image_id = self.upload_reference_image()
            if not self.reference_image_id:
                print("Failed to upload the reference image")
                return False

        # Step 3: Create job
        job_id = self.create_job(file_ids)
        if not job_id:
            return False

        # Step 4: Wait for completion
        status = self.wait_for_job(job_id)
        if status != "completed":
            print(f"Job did not complete successfully (status: {status})")
            return False

        # Step 5: Download results
        if not self.download_results(job_id):
            return False

        print("=" * 70)
        print("Processing complete")
        print("=" * 70)
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Script to recolor a garment consistently across a set of product images"
    )
    parser.add_argument(
        "--input-folder",
        type=str,
        required=True,
        help="Path to folder containing product images (1-10 photos of the same garment)"
    )
    parser.add_argument(
        "--output-folder",
        type=str,
        default="output",
        help="Output folder for results (default: output)"
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)"
    )
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token from https://app.on-model.com/profile?tab=tokens"
    )

    # Recolor target (at least one required)
    target_group = parser.add_argument_group("recolor target (at least one required)")
    target_group.add_argument(
        "--garment-label",
        type=str,
        default=None,
        help="Which garment to recolor, e.g. 't-shirt', 'jacket', 'hat'. Auto-detected when omitted."
    )
    target_group.add_argument(
        "--reference-image",
        type=str,
        default=None,
        help="Path to a colour/pattern reference image to recolor toward (uploaded like any other image)."
    )
    target_group.add_argument(
        "--reference-text",
        type=str,
        default=None,
        help="Colour or pattern as text, e.g. a hex code '#2E5A3B' or a name like 'forest green'."
    )
    target_group.add_argument(
        "--instruction",
        type=str,
        default=None,
        help="Free-form recolor instruction, e.g. 'make it forest green'."
    )

    # Output options
    output_group = parser.add_argument_group("output options")
    output_group.add_argument(
        "--num-variations",
        type=int,
        default=1,
        help="Number of output variations per input image (1-8, default: 1)"
    )
    output_group.add_argument(
        "--processing-size",
        type=int,
        default=2048,
        choices=[1024, 2048, 4096],
        help="Output resolution in pixels: 1024 | 2048 | 4096 (default: 2048). "
             "Drives credit cost: 3 / 5 / 10 credits per output."
    )

    # Generation options
    generation_group = parser.add_argument_group("generation options")
    generation_group.add_argument(
        "--model",
        choices=["auto", "nano_banana_2", "nano_banana_pro", "seedream", "gpt_image"],
        default="auto",
        help="Generation engine: auto | nano_banana_2 | nano_banana_pro | seedream | gpt_image. "
             "'auto' (default) uses the default engine with a safety fallback. "
             "Specifying an engine disables the fallback."
    )
    generation_group.add_argument(
        "--no-use-anchor",
        dest="use_anchor",
        action="store_false",
        default=True,
        help="Disable cross-output consistency. By default the recolor is kept visually "
             "consistent and cohesive across every output in the set."
    )
    generation_group.add_argument(
        "--no-post-process",
        dest="post_process",
        action="store_false",
        default=True,
        help="Disable the finishing pass that keeps the recolored garment tones consistent "
             "across the set. Enabled by default."
    )
    generation_group.add_argument(
        "--add-ai-watermark",
        action="store_true",
        default=False,
        help="Bake an 'AI-generated' disclosure mark into each output (irreversible)."
    )

    args = parser.parse_args()

    # Enforce the API rule client-side: at least one recolor target is required.
    if not args.reference_image and not args.reference_text and not args.instruction:
        parser.error(
            "Provide at least one recolor target: --reference-image, --reference-text, or --instruction"
        )

    processor = GarmentRecolor(
        base_url=args.base_url,
        token=args.token,
        input_folder=args.input_folder,
        output_folder=args.output_folder,
        garment_label=args.garment_label,
        reference_image=args.reference_image,
        reference_text=args.reference_text,
        instruction=args.instruction,
        num_variations=args.num_variations,
        model=args.model,
        processing_size=args.processing_size,
        post_process=args.post_process,
        use_anchor=args.use_anchor,
        add_ai_watermark=args.add_ai_watermark,
    )

    success = processor.run()

    if not success:
        exit(1)


if __name__ == "__main__":
    main()
