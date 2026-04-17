import os
import json
import requests
import logging
import hashlib
from datetime import datetime
from typing import Dict, Optional, Tuple, List, Set

logger = logging.getLogger(__name__)

class WebPageCache:
    """
    Web page content caching mechanism with the following features:
    - Each URL's content is stored as a separate JSON file
    - Uses a mapping file to record URL-to-filename relationships
    - Failed URLs are stored in a separate JSON file
    - Cache failure logs are recorded in a text file
    """
    
    def __init__(self, cache_dir: str = "cache", 
                 url_map_file: str = "url_map.json",
                 failed_urls_file: str = "failed_urls.json",
                 error_log_file: str = "cache_errors.txt",
                 timeout: int = 30):
        """
        Initialize the caching mechanism
        
        Args:
            cache_dir: Cache directory path
            url_map_file: URL mapping filename
            failed_urls_file: Failed URLs storage filename
            error_log_file: Error log filename
            timeout: Request timeout in seconds
        """
        self.cache_dir = cache_dir
        self.content_dir = os.path.join(cache_dir, "content")
        self.url_map_path = os.path.join(cache_dir, url_map_file)
        self.failed_urls_path = os.path.join(cache_dir, failed_urls_file)
        self.error_log_path = os.path.join(cache_dir, error_log_file)
        self.timeout = timeout
        
        # URL to filename mapping
        self.url_map = {}
        
        # Set of failed URLs
        self.failed_urls = {}
        
        # Create necessary directories
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        if not os.path.exists(self.content_dir):
            os.makedirs(self.content_dir)
            
        # Load URL mapping
        if os.path.exists(self.url_map_path):
            try:
                with open(self.url_map_path, 'r', encoding='utf-8') as f:
                    self.url_map = json.load(f)
            except json.JSONDecodeError as e:
                logger.warning("[WebPageCache] Failed to load URL mapping file: %s, %s", self.url_map_path, e)
                self.url_map = {}
        
        # Load failed URLs
        if os.path.exists(self.failed_urls_path):
            try:
                with open(self.failed_urls_path, 'r', encoding='utf-8') as f:
                    self.failed_urls = json.load(f)
            except json.JSONDecodeError as e:
                logger.warning("[WebPageCache] Failed to load failed URLs file: %s, %s", self.failed_urls_path, e)
                self.failed_urls = {}
    
    def get_content(self, url: str, force_refresh: bool = False) -> Tuple[bool, Optional[str]]:
        """
        Get URL content, reading from cache if available, otherwise fetching from web
        
        Args:
            url: URL to fetch
            force_refresh: Whether to force refresh the cache
            
        Returns:
            Tuple of (success_flag, content)
        """
        # Check if URL is in cache and not forcing refresh
        if url in self.url_map and not force_refresh:
            filename = self.url_map[url]
            file_path = os.path.join(self.content_dir, filename)
            
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        logger.debug("[WebPageCache] Cache hit: %s", url)
                        return True, data
                except Exception as e:
                    logger.warning("[WebPageCache] Failed to read cache file: %s, %s: %s", file_path, type(e).__name__, e)

        return False, None     

    @staticmethod
    def _sanitize_error(error_str: str) -> str:
        """Remove sensitive info (e.g. API keys) from error strings."""
        import re
        return re.sub(r'apiKey=[^&\s]+', 'apiKey=***', error_str)

    def store_failed(self, url:str, e:str) -> None:
        """Store a failed URL attempt"""
        if url not in self.failed_urls:
            sanitized_error = self._sanitize_error(str(e))
            logger.warning("[WebPageCache] URL fetch failed: %s, error: %s", url, sanitized_error)
            
            # Add to failed URLs list
            timestamp = datetime.now().isoformat()
            self.failed_urls[url] = {
                "timestamp": timestamp,
                "error": sanitized_error
            }
            self._save_failed_urls()
        else:
            logger.debug("[WebPageCache] %s already exists in cache_fail_log", url)

    def store_content(self, url: str, data: str) -> bool:
        """
        Manually store URL content to cache
        
        Args:
            url: URL to store
            content: Content to store
            
        Returns:
            Whether storage was successful
        """
        try:
            self._store_url_content(url, data)
            
            # Remove from failed list if previously there
            if url in self.failed_urls:
                del self.failed_urls[url]
                self._save_failed_urls()
                
            logger.debug("[WebPageCache] Stored URL content: %s", url)
            return True
        except Exception as e:
            logger.warning("[WebPageCache] Failed to store URL content: %s, %s: %s", url, type(e).__name__, e)
            return False
    
    def _store_url_content(self, url: str, data: str) -> None:
        """
        Store URL content as a separate JSON file
        
        Args:
            url: URL to store
            content: Content to store
        """
        # Generate filename (using URL hash)
        filename = self._get_filename_for_url(url)
        file_path = os.path.join(self.content_dir, filename)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # Update URL mapping
        self.url_map[url] = filename
        self._save_url_map()
    
    def _get_filename_for_url(self, url: str) -> str:
        """
        Generate unique filename for URL
        
        Args:
            url: URL
            
        Returns:
            Filename string
        """
        # Use MD5 hash to generate filename
        hash_obj = hashlib.md5(url.encode('utf-8'))
        return f"{hash_obj.hexdigest()}.json"
    
    def _save_url_map(self) -> None:
        """Save URL mapping to file"""
        try:
            with open(self.url_map_path, 'w', encoding='utf-8') as f:
                json.dump(self.url_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[WebPageCache] Failed to save URL map: %s: %s", type(e).__name__, e)
    
    def _save_failed_urls(self) -> None:
        """Save failed URLs to file"""
        try:
            with open(self.failed_urls_path, 'w', encoding='utf-8') as f:
                json.dump(self.failed_urls, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[WebPageCache] Failed to save failed URLs list: %s: %s", type(e).__name__, e)
    
    def _log_error(self, message: str) -> None:
        """Legacy method - now delegates to standard logger."""
        logger.warning(message)
    
    def get_failed_urls(self) -> Dict[str, Dict]:
        """
        Get list of failed URLs
        
        Returns:
            Dictionary of failed URLs in format {url: {timestamp, error}}
        """
        return self.failed_urls
    
    def retry_failed_urls(self) -> Dict[str, bool]:
        """
        Retry all failed URLs
        
        Returns:
            Dictionary of retry results in format {url: success_status}
        """
        results = {}
        failed_urls_copy = self.failed_urls.copy()
        
        for url in failed_urls_copy:
            success, _ = self.get_content(url, force_refresh=True)
            results[url] = success
        
        return results
    
    def clear_cache(self) -> None:
        """Clear entire cache"""
        # Clear URL mapping
        self.url_map = {}
        self._save_url_map()
        
        # Delete all content files
        for filename in os.listdir(self.content_dir):
            file_path = os.path.join(self.content_dir, filename)
            if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        logger.warning("[WebPageCache] Failed to delete cache file: %s, %s: %s", file_path, type(e).__name__, e)
        
        logger.info("[WebPageCache] Cache cleared")