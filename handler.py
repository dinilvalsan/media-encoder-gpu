import os
import subprocess
import uuid
import json
import boto3
import runpod
from glob import glob
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# --- R2 Configuration ---
try:
    CLOUDFLARE_ACCOUNT_ID = os.environ['CLOUDFLARE_ACCOUNT_ID']
    S3_ACCESS_KEY = os.environ['CLOUDFLARE_R2_ACCESS_KEY_ID']
    S3_SECRET_KEY = os.environ['CLOUDFLARE_R2_SECRET_ACCESS_KEY']
    S3_BUCKET_NAME = os.environ['CLOUDFLARE_R2_BUCKET_NAME']
    S3_ENDPOINT_URL = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    
    # Optional: Public R2 domain for URLs
    R2_PUBLIC_DOMAIN = os.environ.get('R2_PUBLIC_DOMAIN', f"{S3_BUCKET_NAME}.r2.dev")

    s3 = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name='auto'
    )
except KeyError as e:
    print(f"❌ Missing environment variable: {e}")
    s3 = None

# Configuration
USE_GPU = os.environ.get('USE_GPU', 'true').lower() == 'true'
THUMBNAIL_INTERVAL = int(os.environ.get('THUMBNAIL_INTERVAL', '10'))
MAX_UPLOAD_WORKERS = int(os.environ.get('MAX_UPLOAD_WORKERS', '10'))
HLS_SEGMENT_DURATION = int(os.environ.get('HLS_SEGMENT_DURATION', '6'))


def run_command(command, description="Command"):
    """Execute shell command with error handling and real-time output"""
    try:
        print(f"[{description}] Running: {' '.join(command)}")
        start_time = time.time()
        
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True
        )
        
        elapsed = time.time() - start_time
        print(f"[{description}] ✓ Success ({elapsed:.2f}s)")
        return result
    except subprocess.CalledProcessError as e:
        print(f"[{description}] ❌ Failed!")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        raise


def check_gpu_available():
    """Check if NVIDIA GPU is available"""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            gpu_name = result.stdout.strip()
            print(f"✓ GPU detected: {gpu_name}")
            return True
    except:
        pass
    print("⚠️  No GPU detected, falling back to CPU")
    return False


def get_video_info(video_path):
    """Get video duration and basic metadata"""
    try:
        result = run_command([
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration:stream=width,height,codec_name',
            '-of', 'json',
            video_path
        ], "Get Video Info")
        
        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration', 0))
        
        video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
        
        return {
            'duration': duration,
            'width': video_stream.get('width', 0),
            'height': video_stream.get('height', 0),
            'codec': video_stream.get('codec_name', 'unknown')
        }
    except Exception as e:
        print(f"Warning: Could not get video info: {e}")
        return {'duration': 0, 'width': 0, 'height': 0, 'codec': 'unknown'}


def transcode_to_hls(input_path, output_dir, use_gpu=True):
    """
    Transcode to 480p HLS with GPU acceleration (if available)
    """
    print(f"Starting HLS transcoding (GPU: {use_gpu})...")

    playlist_path = os.path.join(output_dir, 'playlist.m3u8')
    segment_pattern = os.path.join(output_dir, 'segment_%03d.ts')

    if use_gpu:
        # NVENC GPU encoding - 5-10x faster
        command = [
            'ffmpeg',
            '-hwaccel', 'cuda',
            '-hwaccel_output_format', 'cuda',
            '-i', input_path,
            '-vf', 'scale_cuda=-2:480',  # GPU-based scaling
            '-c:v', 'h264_nvenc',
            '-preset', 'p4',  # p1 (fastest) to p7 (slowest), p4 is balanced
            '-tune', 'hq',  # High quality tuning
            '-rc', 'vbr',  # Variable bitrate
            '-cq', '23',  # Quality level (lower = better)
            '-b:v', '1200k',
            '-maxrate', '1500k',
            '-bufsize', '3000k',
            '-g', '90',  # GOP size
            '-keyint_min', '90',
            '-spatial_aq', '1',  # Spatial adaptive quantization
            '-temporal_aq', '1',  # Temporal adaptive quantization
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '48000',
            '-ac', '2',
            '-f', 'hls',
            '-hls_time', str(HLS_SEGMENT_DURATION),
            '-hls_list_size', '0',
            '-hls_segment_type', 'mpegts',
            '-hls_flags', 'independent_segments',
            '-hls_segment_filename', segment_pattern,
            '-y',
            playlist_path
        ]
    else:
        # CPU encoding with 'veryfast' preset
        command = [
            'ffmpeg',
            '-i', input_path,
            '-vf', 'scale=-2:480',
            '-c:v', 'libx264',
            '-preset', 'veryfast',  # Much faster than 'medium'
            '-crf', '25',  # Slightly higher CRF for speed
            '-profile:v', 'main',
            '-level', '3.1',
            '-maxrate', '1200k',
            '-bufsize', '2400k',
            '-g', '90',
            '-keyint_min', '90',
            '-sc_threshold', '0',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '48000',
            '-ac', '2',
            '-f', 'hls',
            '-hls_time', str(HLS_SEGMENT_DURATION),
            '-hls_list_size', '0',
            '-hls_segment_type', 'mpegts',
            '-hls_flags', 'independent_segments',
            '-hls_segment_filename', segment_pattern,
            '-y',
            playlist_path
        ]

    run_command(command, "HLS Transcode")

    segments = sorted(glob(os.path.join(output_dir, 'segment_*.ts')))
    print(f"✓ Generated {len(segments)} HLS segments")

    return {
        'playlist_path': playlist_path,
        'segments': segments
    }


def generate_thumbnails(input_path, output_dir, interval=10):
    """
    Generate thumbnails every N seconds
    Uses CPU as thumbnail generation is lightweight
    """
    print(f"Generating thumbnails every {interval} seconds...")

    output_pattern = os.path.join(output_dir, 'thumb_%04d.jpg')

    command = [
        'ffmpeg',
        '-i', input_path,
        '-vf', f'fps=1/{interval},scale=320:-2',  # 320px wide, maintain aspect ratio
        '-q:v', '2',  # Quality 2 (1-31, lower is better)
        '-y',
        output_pattern
    ]

    run_command(command, "Thumbnail Generation")

    thumbnails = sorted(glob(os.path.join(output_dir, 'thumb_*.jpg')))
    print(f"✓ Generated {len(thumbnails)} thumbnails")

    return thumbnails


def upload_file_async(local_path, s3_key, content_type=None):
    """Thread-safe file upload to R2"""
    try:
        file_size = os.path.getsize(local_path)
        extra_args = {}
        
        if content_type:
            extra_args['ContentType'] = content_type
        
        # Add cache control for HLS content
        if s3_key.endswith('.m3u8'):
            extra_args['CacheControl'] = 'max-age=3600'  # 1 hour
        elif s3_key.endswith('.ts'):
            extra_args['CacheControl'] = 'max-age=31536000'  # 1 year (immutable)
        elif s3_key.endswith('.jpg'):
            extra_args['CacheControl'] = 'max-age=31536000'  # 1 year
        
        s3.upload_file(local_path, S3_BUCKET_NAME, s3_key, ExtraArgs=extra_args)
        
        return {
            'key': s3_key,
            'size': file_size,
            'status': 'success'
        }
    except Exception as e:
        print(f"❌ Upload failed for {s3_key}: {e}")
        return {
            'key': s3_key,
            'status': 'error',
            'error': str(e)
        }


def parallel_upload_files(files_to_upload, max_workers=10):
    """
    Upload multiple files in parallel
    files_to_upload: list of (local_path, s3_key, content_type) tuples
    """
    print(f"Uploading {len(files_to_upload)} files with {max_workers} workers...")
    
    upload_results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(upload_file_async, local_path, s3_key, content_type): s3_key
            for local_path, s3_key, content_type in files_to_upload
        }
        
        for future in as_completed(futures):
            result = future.result()
            upload_results.append(result)
            
            if result['status'] == 'success':
                print(f"  ✓ {os.path.basename(result['key'])} ({result['size']/1024:.1f} KB)")
    
    failed = [r for r in upload_results if r['status'] == 'error']
    if failed:
        print(f"⚠️  {len(failed)} uploads failed")
    
    return upload_results


def handler(job):
    """
    RunPod Serverless Handler

    Expected Input:
    {
        "input": {
            "source_video_key": "uploads/video123.mp4"
        }
    }
    
    Returns processing results with HLS playlist, segments, and thumbnails
    """

    if not s3:
        return {
            "status": "error",
            "error": "R2 storage not configured. Check environment variables."
        }

    job_input = job.get('input', {})
    source_video_key = job_input.get('source_video_key')

    if not source_video_key:
        return {
            "status": "error",
            "error": "Missing 'source_video_key' in input"
        }

    job_id = str(uuid.uuid4())
    start_time = datetime.utcnow()
    
    print(f"\n{'='*60}")
    print(f"🎬 Starting Video Processing Job")
    print(f"{'='*60}")
    print(f"Job ID: {job_id}")
    print(f"Source: {source_video_key}")
    print(f"GPU Mode: {USE_GPU}")
    print(f"{'='*60}\n")

    # Setup temp directories
    base_dir = f"/tmp/{job_id}"
    input_dir = f"{base_dir}/input"
    hls_dir = f"{base_dir}/hls"
    thumb_dir = f"{base_dir}/thumbs"

    for d in [input_dir, hls_dir, thumb_dir]:
        os.makedirs(d, exist_ok=True)

    local_input = os.path.join(input_dir, os.path.basename(source_video_key))

    try:
        # Check GPU availability
        gpu_available = check_gpu_available() if USE_GPU else False
        use_gpu = USE_GPU and gpu_available

        # Step 1: Download source video
        print(f"📥 Downloading {source_video_key}...")
        download_start = time.time()
        s3.download_file(S3_BUCKET_NAME, source_video_key, local_input)
        download_time = time.time() - download_start
        file_size_mb = os.path.getsize(local_input) / (1024 * 1024)
        print(f"✓ Downloaded {file_size_mb:.2f} MB in {download_time:.2f}s")

        # Step 2: Get video info
        video_info = get_video_info(local_input)
        print(f"📹 Video: {video_info['width']}x{video_info['height']}, "
              f"{video_info['duration']:.1f}s, {video_info['codec']}")

        # Step 3: Parallel processing - HLS transcoding + Thumbnail generation
        print(f"\n⚙️  Starting parallel processing...")
        process_start = time.time()
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            hls_future = executor.submit(transcode_to_hls, local_input, hls_dir, use_gpu)
            thumb_future = executor.submit(generate_thumbnails, local_input, thumb_dir, THUMBNAIL_INTERVAL)
            
            # Wait for both to complete
            hls_result = hls_future.result()
            thumbnails = thumb_future.result()
        
        process_time = time.time() - process_start
        print(f"✓ Processing completed in {process_time:.2f}s")

        # Step 4: Parallel upload to R2
        print(f"\n☁️  Uploading to R2...")
        upload_start = time.time()
        
        s3_prefix = f"processed/{job_id}"
        files_to_upload = []

        # Prepare playlist upload
        playlist_key = f"{s3_prefix}/hls/playlist.m3u8"
        files_to_upload.append((
            hls_result['playlist_path'],
            playlist_key,
            'application/vnd.apple.mpegurl'
        ))

        # Prepare segment uploads
        segment_keys = []
        for seg in hls_result['segments']:
            seg_name = os.path.basename(seg)
            seg_key = f"{s3_prefix}/hls/{seg_name}"
            segment_keys.append(seg_key)
            files_to_upload.append((seg, seg_key, 'video/mp2t'))

        # Prepare thumbnail uploads
        thumb_keys = []
        for thumb in thumbnails:
            thumb_name = os.path.basename(thumb)
            thumb_key = f"{s3_prefix}/thumbnails/{thumb_name}"
            thumb_keys.append(thumb_key)
            files_to_upload.append((thumb, thumb_key, 'image/jpeg'))

        # Upload all files in parallel
        upload_results = parallel_upload_files(files_to_upload, max_workers=MAX_UPLOAD_WORKERS)
        upload_time = time.time() - upload_start
        
        successful_uploads = [r for r in upload_results if r['status'] == 'success']
        total_uploaded_mb = sum(r['size'] for r in successful_uploads) / (1024 * 1024)
        print(f"✓ Uploaded {len(successful_uploads)} files ({total_uploaded_mb:.2f} MB) in {upload_time:.2f}s")

        # Calculate total time
        end_time = datetime.utcnow()
        total_time = (end_time - start_time).total_seconds()

        print(f"\n{'='*60}")
        print(f"✅ Job Completed Successfully")
        print(f"{'='*60}")
        print(f"Total Time: {total_time:.2f}s")
        print(f"  Download: {download_time:.2f}s")
        print(f"  Processing: {process_time:.2f}s")
        print(f"  Upload: {upload_time:.2f}s")
        print(f"{'='*60}\n")

        # Return structured response
        return {
            "status": "success",
            "job_id": job_id,
            "timing": {
                "total_seconds": round(total_time, 2),
                "download_seconds": round(download_time, 2),
                "processing_seconds": round(process_time, 2),
                "upload_seconds": round(upload_time, 2)
            },
            "source": {
                "key": source_video_key,
                "size_mb": round(file_size_mb, 2),
                "duration_seconds": round(video_info['duration'], 2),
                "resolution": f"{video_info['width']}x{video_info['height']}",
                "codec": video_info['codec']
            },
            "hls": {
                "playlist_key": playlist_key,
                "playlist_url": f"https://{R2_PUBLIC_DOMAIN}/{playlist_key}",
                "segment_count": len(segment_keys),
                "segment_duration": HLS_SEGMENT_DURATION,
                "segment_keys": segment_keys
            },
            "thumbnails": {
                "count": len(thumb_keys),
                "interval_seconds": THUMBNAIL_INTERVAL,
                "keys": thumb_keys,
                "urls": [f"https://{R2_PUBLIC_DOMAIN}/{k}" for k in thumb_keys]
            },
            "processing": {
                "gpu_used": use_gpu,
                "gpu_available": gpu_available
            },
            "storage_prefix": s3_prefix
        }

    except Exception as e:
        error_time = (datetime.utcnow() - start_time).total_seconds()
        print(f"\n{'='*60}")
        print(f"❌ Job Failed")
        print(f"{'='*60}")
        print(f"Error: {str(e)}")
        print(f"{'='*60}\n")
        
        import traceback
        traceback.print_exc()

        return {
            "status": "error",
            "job_id": job_id,
            "error": str(e),
            "error_type": type(e).__name__,
            "processing_time_seconds": round(error_time, 2)
        }

    finally:
        # Cleanup temp files
        print("🧹 Cleaning up temporary files...")
        try:
            import shutil
            if os.path.exists(base_dir):
                shutil.rmtree(base_dir)
                print("✓ Cleanup complete")
        except Exception as e:
            print(f"⚠️  Cleanup warning: {e}")


# Start RunPod worker
if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 RunPod Video Processing Worker Starting")
    print("="*60)
    print(f"GPU Mode: {USE_GPU}")
    print(f"Thumbnail Interval: {THUMBNAIL_INTERVAL}s")
    print(f"HLS Segment Duration: {HLS_SEGMENT_DURATION}s")
    print(f"Max Upload Workers: {MAX_UPLOAD_WORKERS}")
    print(f"R2 Bucket: {S3_BUCKET_NAME}")
    print("="*60 + "\n")
    
    runpod.serverless.start({"handler": handler})