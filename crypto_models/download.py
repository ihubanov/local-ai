import os
import random
import requests
import aiohttp
import asyncio
import time
from pathlib import Path
from crypto_models.utils import compute_file_hash, async_extract_zip, async_move, async_rmtree

# Constants
GATEWAY_URL = "https://gateway.mesh3.network/ipfs/"
DEFAULT_OUTPUT_DIR = Path.cwd() / "llms-storage"
SLEEP_TIME = 60
MAX_ATTEMPTS = 2
POSTFIX_MODEL_PATH = ".gguf"

# Performance optimizations
CHUNK_SIZE_MB = 4  # Increased from 1MB to 4MB
FLUSH_INTERVAL_MB = 50  # Increased from 10MB to 50MB
PROGRESS_BATCH_SIZE = 10 * 1024 * 1024  # Batch progress updates for 10MB chunks
MAX_CONCURRENT_DOWNLOADS = 16  # Increased from 4
CONNECTION_POOL_SIZE = 32  # Increased connection pool

def check_downloaded_model(filecoin_hash: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> bool:
    """
    Check if the model is already downloaded and optionally save metadata.
    
    Args:
        filecoin_hash: IPFS hash of the model metadata
        output_file: Optional path to save metadata JSON
    
    Returns:
        bool: Whether the model is already downloaded
    """    
    try:
        local_path = output_dir / f"{filecoin_hash}{POSTFIX_MODEL_PATH}"
        local_path = local_path.absolute()
        
        # Check if model exists
        is_downloaded = local_path.exists()
            
        if is_downloaded:
            print(f"Model already exists at: {local_path}")
            
        return is_downloaded
        
    except requests.RequestException as e:
        print(f"Failed to fetch model metadata: {e}")
        return False

async def download_single_file_async(session: aiohttp.ClientSession, file_info: dict, folder_path: Path, max_attempts: int = MAX_ATTEMPTS, progress_callback=None, progress_tracker=None) -> tuple:
    """
    Asynchronously download a single file and verify its SHA256 hash, with retries.

    Args:
        session (aiohttp.ClientSession): Reusable HTTP session.
        file_info (dict): Contains 'cid', 'file_hash', and 'file_name'.
        folder_path (Path): Directory to save the file.
        max_attempts (int): Number of retries on failure.
        progress_callback: Optional callback for progress updates.

    Returns:
        tuple: (Path to file if successful, None) or (None, error message).
    """
    cid = file_info["cid"]
    expected_hash = file_info["file_hash"]
    file_name = file_info["file_name"]
    file_path = folder_path / file_name
    attempts = 0
    
    # Use a simple temp file for atomic operations
    temp_path = folder_path / f"{file_name}.tmp"
    
    # Check if file already exists with correct hash
    if file_path.exists():
        try:
            computed_hash = compute_file_hash(file_path)
            if computed_hash == expected_hash:
                print(f"File {cid} already exists with correct hash.")
                return file_path, None
            else:
                print(f"File {cid} exists but hash mismatch. Retrying...")
                file_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error checking existing file {cid}: {e}")
            file_path.unlink(missing_ok=True)

    # Clean up any existing temp file
    if temp_path.exists():
        temp_path.unlink(missing_ok=True)

    while attempts < max_attempts:
        try:
            url = f"{GATEWAY_URL}{cid}"
            
            # Use optimized chunk size for faster downloads
            chunk_size = CHUNK_SIZE_MB * 1024 * 1024  # 4MB chunks
            
            # Use a longer timeout for large files
            timeout = aiohttp.ClientTimeout(total=900, connect=120, sock_read=300, sock_connect=120)
            
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    # Get total size for progress tracking
                    total_size = int(response.headers.get("content-length", 0))
                    
                    # Report file size to progress tracker
                    if progress_tracker and total_size > 0:
                        await progress_tracker.add_file_size(total_size)
                    
                    # Download to temp file
                    with temp_path.open("wb") as f:
                        # Add timeout protection for each chunk
                        last_data_time = asyncio.get_event_loop().time()
                        chunk_timeout = 180  # 180 seconds without data is a timeout
                        bytes_written = 0
                        
                        # Download with per-chunk timeout protection
                        async for chunk in response.content.iter_chunked(chunk_size):
                            # Reset timeout timer when data is received
                            last_data_time = asyncio.get_event_loop().time()
                            
                            # Write chunk
                            f.write(chunk)
                            bytes_written += len(chunk)
                            
                            # Report progress to overall tracker with batched updates
                            if progress_callback:
                                await progress_callback(len(chunk))
                            
                            # Flush to disk less frequently for better performance
                            if bytes_written >= FLUSH_INTERVAL_MB * 1024 * 1024:  # 50MB
                                f.flush()
                                os.fsync(f.fileno())
                                bytes_written = 0
                            
                            # Check if download has been idle
                            current_time = asyncio.get_event_loop().time()
                            if current_time - last_data_time > chunk_timeout:
                                raise asyncio.TimeoutError(f"No data received for {chunk_timeout} seconds")
                        
                        # Final flush
                        f.flush()
                        os.fsync(f.fileno())

                    # Verify hash
                    computed_hash = compute_file_hash(temp_path)
                    if computed_hash == expected_hash:
                        # Rename temp file to final file only after successful verification
                        if file_path.exists():
                            file_path.unlink()
                        temp_path.rename(file_path)
                        print(f"File {cid} downloaded and verified successfully.")
                        return file_path, None
                    else:
                        print(f"Hash mismatch for {cid}. Expected {expected_hash}, got {computed_hash}.")
                        # Clean up temp file on hash mismatch
                        temp_path.unlink(missing_ok=True)
                else:
                    print(f"Failed to download {cid}. Status: {response.status}")
                    # For certain status codes, we might want to fail faster
                    if response.status in (404, 403, 401):
                        wait_time = SLEEP_TIME
                    else:
                        wait_time = min(SLEEP_TIME * (2 ** attempts), 300)
            
        except asyncio.TimeoutError:
            print(f"Timeout downloading {cid}")
            # Clean up temp file on timeout
            temp_path.unlink(missing_ok=True)
            wait_time = min(SLEEP_TIME * (2 ** attempts), 300)
        except aiohttp.ClientError as e:
            print(f"Client error downloading {cid}: {e}")
            temp_path.unlink(missing_ok=True)
            wait_time = min(SLEEP_TIME * (2 ** attempts), 300)
        except Exception as e:
            print(f"Exception downloading {cid}: {e}")
            temp_path.unlink(missing_ok=True)
            wait_time = min(SLEEP_TIME * (2 ** attempts), 300)

        attempts += 1
        if attempts < max_attempts:
            print(f"Retrying in {wait_time}s (Attempt {attempts + 1}/{max_attempts})")
            await asyncio.sleep(wait_time)
        else:
            print(f"Failed to download {cid} after {max_attempts} attempts.")
            # Clean up temp file on final failure
            temp_path.unlink(missing_ok=True)
            return None, f"Failed to download {cid} after {max_attempts} attempts."

class ProgressTracker:
    """Track download progress across multiple concurrent downloads with batched updates"""
    
    def __init__(self, total_files: int, filecoin_hash: str):
        self.total_files = total_files
        self.filecoin_hash = filecoin_hash
        self.total_bytes_downloaded = 0
        self.total_bytes_expected = 0
        self.completed_files = 0
        self.start_time = time.time()
        self.last_log_time = 0
        self.lock = asyncio.Lock()
        
        # Batched progress tracking to reduce lock contention
        self.pending_bytes = 0
        self.pending_lock = asyncio.Lock()
        
        # Start background task for periodic progress updates
        self.progress_task = asyncio.create_task(self._periodic_progress_update())
        
    async def add_file_size(self, file_size: int):
        """Add expected file size to total"""
        async with self.lock:
            self.total_bytes_expected += file_size
    
    async def update_progress_batched(self, bytes_downloaded: int):
        """Update progress using batched approach to reduce lock contention"""
        # Use a separate lock for pending bytes to minimize contention
        async with self.pending_lock:
            self.pending_bytes += bytes_downloaded
            
            # Only acquire main lock if we have enough pending bytes
            if self.pending_bytes >= PROGRESS_BATCH_SIZE:
                pending_to_process = self.pending_bytes
                self.pending_bytes = 0
                
                # Quick update to main counter
                async with self.lock:
                    self.total_bytes_downloaded += pending_to_process
    
    # Compatibility method for backward compatibility
    async def update_progress(self, bytes_downloaded: int):
        """Backward compatibility wrapper"""
        await self.update_progress_batched(bytes_downloaded)
    
    async def _periodic_progress_update(self):
        """Background task to log progress periodically"""
        while True:
            try:
                await asyncio.sleep(1.0)  # Check every second
                
                # Process any remaining pending bytes
                async with self.pending_lock:
                    if self.pending_bytes > 0:
                        pending_to_process = self.pending_bytes
                        self.pending_bytes = 0
                        
                        async with self.lock:
                            self.total_bytes_downloaded += pending_to_process
                
                # Log progress every 2 seconds
                current_time = time.time()
                if current_time - self.last_log_time >= 2.0:
                    async with self.lock:
                        self.last_log_time = current_time
                        elapsed_time = current_time - self.start_time
                        speed_mbps = (self.total_bytes_downloaded / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0
                        
                        # Calculate percentage based on bytes downloaded vs total expected
                        if self.total_bytes_expected > 0:
                            percentage = (self.total_bytes_downloaded / self.total_bytes_expected) * 100
                            percentage = min(percentage, 100.0)  # Cap at 100%
                        else:
                            # Fall back to file-based progress if no total size available
                            percentage = (self.completed_files / self.total_files) * 100
                        
                        print(f"[CRYPTOAGENTS_LOGGER] [MODEL_INSTALL] --progress {percentage:.1f}% ({self.completed_files}/{self.total_files} files) --speed {speed_mbps:.2f} MB/s")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in progress update task: {e}")
    
    async def complete_file(self):
        """Mark a file as completed"""
        async with self.lock:
            self.completed_files += 1
            elapsed_time = time.time() - self.start_time
            speed_mbps = (self.total_bytes_downloaded / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0
            
            # Calculate percentage based on bytes downloaded vs total expected
            if self.total_bytes_expected > 0:
                percentage = (self.total_bytes_downloaded / self.total_bytes_expected) * 100
                percentage = min(percentage, 100.0)  # Cap at 100%
            else:
                # Fall back to file-based progress if no total size available
                percentage = (self.completed_files / self.total_files) * 100
            
            print(f"[CRYPTOAGENTS_LOGGER] [MODEL_INSTALL] --progress {percentage:.1f}% ({self.completed_files}/{self.total_files} files) --speed {speed_mbps:.2f} MB/s")
    
    async def cleanup(self):
        """Clean up background tasks"""
        if hasattr(self, 'progress_task'):
            self.progress_task.cancel()
            try:
                await self.progress_task
            except asyncio.CancelledError:
                pass

async def download_files_from_lighthouse_async(data: dict) -> list:
    """
    Asynchronously download files concurrently using Filecoin CIDs and verify hashes.

    Args:
        data (dict): JSON data with 'folder_name', 'files', 'num_of_files', and 'filecoin_hash'.

    Returns:
        list: Paths of successfully downloaded files, or empty list if failed.
    """
    folder_name = data["folder_name"]
    folder_path = Path(folder_name)
    folder_path.mkdir(exist_ok=True)
    num_of_files = data["num_of_files"]
    filecoin_hash = data["filecoin_hash"]
    files = data["files"]
    
    # Calculate total size for progress indication
    total_files = len(files)
    
    # Use optimized concurrency limits
    max_concurrent_downloads = min(MAX_CONCURRENT_DOWNLOADS, max(4, num_of_files))
    semaphore = asyncio.Semaphore(max_concurrent_downloads)
    
    # Create progress tracker
    progress_tracker = ProgressTracker(total_files, filecoin_hash)
    
    # Wrapper for download with semaphore
    async def download_with_semaphore(session, file_info, folder_path):
        async with semaphore:
            return await download_single_file_async(
                session, file_info, folder_path, 
                progress_callback=progress_tracker.update_progress_batched,
                progress_tracker=progress_tracker
            )
    
    # Use larger connection pool for better performance
    connector = aiohttp.TCPConnector(limit=CONNECTION_POOL_SIZE, ssl=False)
    timeout = aiohttp.ClientTimeout(total=None, connect=120, sock_connect=120, sock_read=300)
    
    print(f"Downloading {total_files} files with max {max_concurrent_downloads} concurrent downloads")
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Create tasks
        tasks = [
            download_with_semaphore(session, file_info, folder_path)
            for file_info in files
        ]
        
        # Track overall progress
        successful_downloads = []
        failed_downloads = []
        
        # Use as_completed to process files as they complete
        for i, future in enumerate(asyncio.as_completed(tasks), 1):
            try:
                path, error = await future
                if path:
                    successful_downloads.append(path)
                    await progress_tracker.complete_file()
                else:
                    failed_downloads.append(error)
                    print(f"Download failed: {error}")
            except Exception as e:
                print(f"Unexpected error in download task: {e}")
                failed_downloads.append(str(e))
        
        # Clean up progress tracker
        await progress_tracker.cleanup()
        
        # Check if all downloads were successful
        if len(successful_downloads) == num_of_files:
            print(f"All {num_of_files} files downloaded successfully.")
            return successful_downloads
        else:
            print(f"Downloaded {len(successful_downloads)} out of {num_of_files} files.")
            if failed_downloads:
                print(f"Failed downloads ({len(failed_downloads)}):")
                for i, error in enumerate(failed_downloads[:5], 1):
                    print(f"  {i}. {error}")
                if len(failed_downloads) > 5:
                    print(f"  ... and {len(failed_downloads) - 5} more errors")
            return successful_downloads if successful_downloads else []

async def download_model_from_filecoin_async(filecoin_hash: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> str | None:
    """
    Asynchronously download a model from Filecoin using its IPFS hash.

    Args:
        filecoin_hash (str): IPFS hash of the model metadata.
        output_dir (Path): Directory to save the downloaded model.

    Returns:
        str | None: Path to the downloaded model if successful, None otherwise.
    """
    # Ensure output directory exists
    output_dir.mkdir(exist_ok=True, parents=True)
    local_path = output_dir / f"{filecoin_hash}{POSTFIX_MODEL_PATH}"
    local_path_str = str(local_path.absolute())

    # Check if model is already downloaded
    if check_downloaded_model(filecoin_hash, output_dir):
        print(f"Using existing model at {local_path_str}")
        return local_path_str

    # Define input link
    input_link = f"{GATEWAY_URL}{filecoin_hash}"
    
    # Setup more robust session parameters with optimized limits
    timeout = aiohttp.ClientTimeout(total=180, connect=60)
    connector = aiohttp.TCPConnector(limit=CONNECTION_POOL_SIZE, ssl=False)
    
    # Initialize variables outside the loop
    folder_path = None
    
    try:
        # Use exponential backoff for retries
        for attempt in range(1, MAX_ATTEMPTS + 1):
            backoff = min(SLEEP_TIME * (2 ** (attempt - 1)), 300)
            
            try:
                print(f"Downloading model metadata (attempt {attempt}/{MAX_ATTEMPTS})")
                
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.get(input_link) as response:
                        if response.status != 200:
                            print(f"Failed to fetch metadata: HTTP {response.status}")
                            if attempt < MAX_ATTEMPTS:
                                print(f"Retrying in {backoff} seconds")
                                await asyncio.sleep(backoff)
                                continue
                            else:
                                raise Exception(f"Failed to fetch metadata after {MAX_ATTEMPTS} attempts")
                        
                        # Parse metadata
                        data = await response.json()
                        data["filecoin_hash"] = filecoin_hash
                        folder_name = data["folder_name"]
                        folder_path = Path.cwd() / folder_name
                        
                        # Create folder if it doesn't exist
                        folder_path.mkdir(exist_ok=True, parents=True)
                
                # Download files
                paths = await download_files_from_lighthouse_async(data)
                if not paths:
                    print("Failed to download model files")
                    if attempt < MAX_ATTEMPTS:
                        print(f"Retrying in {backoff} seconds")
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        raise Exception("Failed to download model files after all attempts")
                
                # Extract files
                try:
                    print("Extracting downloaded files...")
                    await async_extract_zip(paths)
                except Exception as e:
                    print(f"Failed to extract files: {e}")
                    if attempt < MAX_ATTEMPTS:
                        print(f"Retrying in {backoff} seconds")
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        raise Exception(f"Failed to extract files after {MAX_ATTEMPTS} attempts: {e}")
                
                # Move files to final location
                try:
                    source_text_path = folder_path / folder_name
                    source_text_path = source_text_path.absolute()
                    print(f"Moving model to {local_path_str}")
                    
                    if source_text_path.exists():
                        # Handle projector path for multimodal models
                        source_projector_path = folder_path / (folder_name + "-projector")
                        source_projector_path = source_projector_path.absolute()
                        
                        if source_projector_path.exists():
                            projector_dest = local_path_str + "-projector"
                            print(f"Moving projector to {projector_dest}")
                            await async_move(str(source_projector_path), projector_dest)
                        
                        # Move model to final location
                        await async_move(str(source_text_path), local_path_str)
                        
                        # Clean up folder after successful move
                        if folder_path.exists():
                            print(f"Cleaning up temporary folder {folder_path}")
                            await async_rmtree(str(folder_path))
                        
                        print(f"Model download complete: {local_path_str}")
                        return local_path_str
                    else:
                        print(f"Model not found at {source_text_path}")
                        if attempt < MAX_ATTEMPTS:
                            print(f"Retrying in {backoff} seconds")
                            await asyncio.sleep(backoff)
                            continue
                        else:
                            raise Exception(f"Model not found at {source_text_path} after all attempts")
                except Exception as e:
                    print(f"Failed to move model: {e}")
                    if attempt < MAX_ATTEMPTS:
                        print(f"Retrying in {backoff} seconds")
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        raise Exception(f"Failed to move model after {MAX_ATTEMPTS} attempts: {e}")
            
            except aiohttp.ClientError as e:
                print(f"HTTP error on attempt {attempt}: {e}")
                if attempt < MAX_ATTEMPTS:
                    print(f"Retrying in {backoff} seconds")
                    await asyncio.sleep(backoff)
                    continue
                else:
                    raise Exception(f"HTTP error after {MAX_ATTEMPTS} attempts: {e}")
            except Exception as e:
                print(f"Download attempt {attempt} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    print(f"Retrying in {backoff} seconds")
                    await asyncio.sleep(backoff)
                    continue
                else:
                    raise Exception(f"Download failed after {MAX_ATTEMPTS} attempts: {e}")
    
    except Exception as e:
        print(f"Download failed: {e}")

    print("All download attempts failed")
    return None