"""
SEC BDC Filing Downloader

This script downloads 10-K and 10-Q filings for specified BDC (Business Development Company) 
CIKs from the SEC EDGAR database and organizes them into a structured folder hierarchy.

Structure: BDC Footnotes/{CIK}/{FILING_TYPE}/{ACCESSION_NUMBER}/files...
"""

import os
import shutil
import logging
from typing import List
from datetime import datetime
from sec_edgar_downloader import Downloader


class BDCFilingDownloader:
    """
    A class to download and organize SEC filings for Business Development Companies.
    
    This downloader fetches 10-K and 10-Q filings from the SEC EDGAR database,
    then reorganizes them into a clean folder structure for easy access.
    """
    
    def __init__(self, company_name: str, email: str, save_path: str = "BDC Footnotes"):
        """
        Initialize the BDC Filing Downloader.
        
        Args:
            company_name: Your company or personal name (required by SEC)
            email: Your email address (required by SEC)
            save_path: Base directory where filings will be saved
        
        Why we need this:
            The SEC requires identification in the User-Agent header to track
            who is accessing their data. This is a legal requirement.
        """
        self.company_name = company_name
        self.email = email
        self.save_path = save_path
        
        # Initialize the SEC downloader with our credentials
        self.downloader = Downloader(company_name, email, save_path)
        
        # Set up logging to track progress and errors
        self._setup_logging()
        
    def _setup_logging(self):
        """
        Configure logging to both file and console.
        
        Why we do this:
            Logging helps us track what's happening during downloads, especially
            useful when downloading many files or debugging issues.
        """
        # Create logs directory if it doesn't exist
        log_dir = os.path.join(self.save_path, "logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # Create a log file with timestamp
        log_file = os.path.join(log_dir, f"download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        
        # Configure logging format and handlers
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),  # Save to file
                logging.StreamHandler()  # Also print to console
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def download_filings(self, ciks: List[str], filing_types: List[str], 
                        start_date: str = "2015-01-01"):
        """
        Download SEC filings for specified CIKs and filing types.
        
        Args:
            ciks: List of CIK numbers (Central Index Key - SEC company identifier)
            filing_types: List of filing types to download (e.g., ["10-K", "10-Q"])
            start_date: Download filings from this date forward (YYYY-MM-DD format)
        
        How this works:
            1. Loop through each CIK (company)
            2. For each company, download each filing type
            3. The sec_edgar_downloader library handles the actual HTTP requests
            4. Files are initially saved in a default structure
        """
        self.logger.info(f"Starting download for {len(ciks)} CIKs and {len(filing_types)} filing types")
        self.logger.info(f"Date range: {start_date} to present")
        
        total_downloads = 0
        
        for cik in ciks:
            for filing_type in filing_types:
                try:
                    self.logger.info(f"Downloading {filing_type} filings for CIK: {cik}")
                    
                    # Download filings with detailed information
                    # download_details=True gets the full filing, not just the index
                    num_downloaded = self.downloader.get(
                        filing_type, 
                        cik, 
                        after=start_date, 
                        download_details=True
                    )
                    
                    total_downloads += num_downloaded
                    self.logger.info(f"Successfully downloaded {num_downloaded} {filing_type} filings for CIK {cik}")
                    
                except Exception as e:
                    # Log errors but continue with other downloads
                    self.logger.error(f"Error downloading {filing_type} for CIK {cik}: {str(e)}")
                    continue
        
        self.logger.info(f"Total filings downloaded: {total_downloads}")
        return total_downloads
    
    def reorganize_files(self):
        """
        Reorganize downloaded files from default structure to custom structure.
        
        Default structure: BDC Footnotes/sec-edgar-filings/{CIK}/{TYPE}/{ACCESSION}/
        Target structure:  BDC Footnotes/{CIK}/{TYPE}/{ACCESSION}/
        
        Why we reorganize:
            The library saves files in a nested 'sec-edgar-filings' folder.
            We want a cleaner structure that's easier to navigate and use.
        
        How this works:
            1. Find the default 'sec-edgar-filings' folder
            2. Walk through all CIK/TYPE/ACCESSION folders
            3. Move files to our preferred structure
            4. Delete the now-empty default folder
        """
        source_dir = os.path.join(self.save_path, "sec-edgar-filings")
        
        # Check if the default download folder exists
        if not os.path.exists(source_dir):
            self.logger.warning("No 'sec-edgar-filings' folder found. Nothing to reorganize.")
            return
        
        self.logger.info("Starting file reorganization...")
        files_moved = 0
        
        try:
            # Walk through the directory structure
            for cik in os.listdir(source_dir):
                cik_path = os.path.join(source_dir, cik)
                
                # Skip if not a directory
                if not os.path.isdir(cik_path):
                    continue
                
                for filing_type in os.listdir(cik_path):
                    type_path = os.path.join(cik_path, filing_type)
                    
                    if not os.path.isdir(type_path):
                        continue
                    
                    for accession in os.listdir(type_path):
                        # Create the target directory structure
                        target_dir = os.path.join(self.save_path, cik, filing_type, accession)
                        os.makedirs(target_dir, exist_ok=True)
                        
                        # Move all files from source to target
                        source_accession_path = os.path.join(type_path, accession)
                        
                        if not os.path.isdir(source_accession_path):
                            continue
                        
                        for filename in os.listdir(source_accession_path):
                            source_file = os.path.join(source_accession_path, filename)
                            target_file = os.path.join(target_dir, filename)
                            
                            # Move the file
                            shutil.move(source_file, target_file)
                            files_moved += 1
            
            # Clean up the now-empty default folder
            shutil.rmtree(source_dir)
            self.logger.info(f"Reorganization complete. Moved {files_moved} files.")
            self.logger.info(f"Cleaned up temporary 'sec-edgar-filings' folder.")
            
        except Exception as e:
            self.logger.error(f"Error during reorganization: {str(e)}")
            raise
    
    def run(self, ciks: List[str], filing_types: List[str], start_date: str = "2015-01-01"):
        """
        Main method to download and organize filings.
        
        This is the primary entry point that:
        1. Downloads all requested filings
        2. Reorganizes them into the clean structure
        3. Returns a summary of what was done
        
        Args:
            ciks: List of CIK numbers to download
            filing_types: List of filing types (e.g., ["10-K", "10-Q"])
            start_date: Start date for downloads (YYYY-MM-DD)
        
        Returns:
            Number of filings downloaded
        """
        self.logger.info("=" * 60)
        self.logger.info("BDC Filing Downloader Started")
        self.logger.info("=" * 60)
        
        try:
            # Step 1: Download filings
            num_downloaded = self.download_filings(ciks, filing_types, start_date)
            
            # Step 2: Reorganize into clean structure
            self.reorganize_files()
            
            self.logger.info("=" * 60)
            self.logger.info("Download and organization complete!")
            self.logger.info(f"Total filings processed: {num_downloaded}")
            self.logger.info(f"Files saved to: {os.path.abspath(self.save_path)}")
            self.logger.info("=" * 60)
            
            return num_downloaded
            
        except Exception as e:
            self.logger.error(f"Fatal error: {str(e)}")
            raise


def main():
    """
    Main execution function with configuration.
    
    This is where you configure:
    - Your identification (required by SEC)
    - Which BDCs to download (CIK numbers)
    - Which filing types to get
    - Date range for downloads
    """
    
    # CONFIGURATION
    COMPANY_NAME = "Alexandre Cela"
    EMAIL = "alexandrecelap@gmail.com"
    SAVE_PATH = "BDC Footnotes"
    
    # All 60 BDC CIK numbers
    BDC_CIKS = [
        "2037804", "1993402", "1989817", "1930087", "1925309",
        "1920145", "1918712", "1913724", "1911066", "1901164",
        "1885968", "1872371", "1869453", "1859919", "1851322",
        "1849894", "1838126", "1837532", "1834543", "1825384",
        "1825265", "1825248", "1812554", "1803498", "1782524",
        "1772704", "1747777", "1747172", "1742313", "1737924",
        "1736035", "1702510", "1675033", "1655888", "1655887",
        "1633336", "1578348", "1572694", "1571329", "1544206",
        "1534254", "1513363", "1512931", "1504619", "1501729",
        "1496099", "1487918", "1487428", "1476765", "1422183",
        "1418076", "1396440", "1379785", "1370755", "1287750",
        "1287032", "1280784", "1278752", "1143513", "17313",
    ]
    
    # Filing types to download
    FILING_TYPES = ["10-K", "10-Q"]
    
    # Download filings from this date forward
    START_DATE = "2015-01-01"
    
    # Create downloader instance
    downloader = BDCFilingDownloader(COMPANY_NAME, EMAIL, SAVE_PATH)
    
    # Run the download and organization process
    downloader.run(BDC_CIKS, FILING_TYPES, START_DATE)


if __name__ == "__main__":
    main()
