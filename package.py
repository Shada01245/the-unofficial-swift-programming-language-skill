#!/usr/bin/env python3
"""
Package The Swift Programming Language into a Claude Skill.

This tool automates the creation of a structured Claude Skill directory
containing the official Swift programming language documentation. It handles
repository cloning, content organization, metadata extraction, and index
generation.

Design Philosophy:
This script is designed to be a standalone, portable generator. It strictly
handles content processing and skill generation. It does NOT manage release
lifecycles, versioning schemes, or marketplace metadata. Those concerns are
handled by the repository's release workflow.

Features:
- One file
- Zero external dependencies
- Robust error handling and cleanup
- Markdown metadata extraction

Usage:
    python3 package.py [--output DIR] [--keep-temp]

Copyright (c) 2025 Kyle Hughes. All rights reserved.
"""

import argparse
import dataclasses
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Pattern, Any


# --- Logging Configuration ---

class TerseFormatter(logging.Formatter):
    def format(self, record):
        return f"[{record.levelname}] {record.msg}"

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(TerseFormatter())

logger = logging.getLogger("swift-packager")
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.propagate = False


# --- Configuration ---

@dataclasses.dataclass
class Configuration:
    """Runtime configuration and constants."""
    
    # Runtime settings
    output_path: Path
    keep_temp: bool = False
    dry_run: bool = False
    
    # Constants
    REPO_URL: str = "https://github.com/swiftlang/swift-book.git"
    SKILL_NAME: str = "programming-swift"
    TOC_REL_PATH: str = "TSPL.docc/The-Swift-Programming-Language.md"
    
    # Content mapping (Section Name -> Directory Name)
    SECTIONS: List[str] = dataclasses.field(default_factory=lambda: [
        'GuidedTour',
        'LanguageGuide',
        'ReferenceManual'
    ])

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Configuration':
        """Create configuration from parsed arguments."""
        output = Path(args.output) if args.output else Path.cwd() / cls.SKILL_NAME
        return cls(
            output_path=output,
            keep_temp=args.keep_temp,
            dry_run=args.dry_run
        )


# --- Git Operations ---

class GitRepository:
    """
    Manages a temporary git repository clone.
    
    Implements the Context Manager protocol for automatic cleanup.
    """

    def __init__(self, url: str, keep_temp: bool = False):
        self.url = url
        self.keep_temp = keep_temp
        self._temp_dir: Optional[str] = None
        self.path: Optional[Path] = None

    def __enter__(self) -> 'GitRepository':
        self._clone()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._cleanup()

    def _clone(self) -> None:
        """Clone the repository to a temporary directory."""
        self._temp_dir = tempfile.mkdtemp(prefix="swift-skill-")
        self.path = Path(self._temp_dir) / "repo"
        
        logger.info(f"Cloning repository from {self.url}...")
        
        cmd = ["git", "clone", "--depth", "1", self.url, str(self.path)]
        
        try:
            result = subprocess.run(
                cmd, 
                check=True, 
                capture_output=True, 
                text=True
            )
            # No verbose logging of clone output
        except subprocess.CalledProcessError as e:
            logger.error(f"Git clone failed: {e.stderr.strip()}")
            self._cleanup()
            raise RuntimeError(f"Failed to clone repository: {e.stderr}") from e
        except FileNotFoundError:
            self._cleanup()
            raise RuntimeError("Git command not found. Please ensure git is installed.")
        
        logger.info("Repository cloned successfully")

    def _cleanup(self) -> None:
        """Remove the temporary directory unless keep_temp is True."""
        if self.keep_temp:
            # User requested to keep the temp directory for inspection
            logger.info(f"Temp directory retained: {self._temp_dir}")
            return

        if self._temp_dir and os.path.exists(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up temp dir: {e}")


# --- Content Processing ---

@dataclasses.dataclass
class DocumentMetadata:
    """Metadata extracted from a markdown file."""
    filename: str
    section: str
    title: str
    description: str
    path: Path


class ContentParser:
    """Parses markdown content, extracts metadata, and handles TOC parsing."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.tspl_root = root_path / "TSPL.docc"

    def get_version(self, toc_rel_path: str) -> Optional[str]:
        """Extract version from the main TOC file."""
        toc_path = self.root_path / toc_rel_path
        if not toc_path.exists():
            logger.warning(f"TOC file not found at {toc_path}")
            return None

        content = toc_path.read_text(encoding='utf-8')
        # Check for version string in the main TOC file to tag the skill
        # This ensures the skill metadata reflects the actual documentation version
        match = re.search(r'^#\s+The Swift Programming Language\s+\(([^)]+)\)', content, re.MULTILINE)
        return match.group(1) if match else None

    def parse_toc_order(self, toc_rel_path: str) -> List[str]:
        """Parse the TOC file to determine the correct document order."""
        toc_path = self.root_path / toc_rel_path
        if not toc_path.exists():
            raise FileNotFoundError(f"TOC file not found: {toc_path}")

        content = toc_path.read_text(encoding='utf-8')
        # Extract all document references to build the ordered list
        return re.findall(r'<doc:([^>]+)>', content)

    def extract_metadata(self, file_path: Path, section: str) -> DocumentMetadata:
        """
        Extract title and description from a markdown file.
        
        Strategy:
        1. Title: First level-1 header (# Title)
        2. Description: First paragraph of text that isn't metadata or empty.
        """
        content = file_path.read_text(encoding='utf-8')
        lines = content.splitlines()
        
        title = file_path.stem
        description_lines = []
        
        # State machine flags
        in_metadata_block = False
        found_title = False
        
        for line in lines:
            stripped = line.strip()
            
            # 1. Handle Metadata Blocks
            # DocC metadata blocks start with @ and must be skipped to avoid
            # including them in the description or title logic.
            if stripped.startswith('@'):
                in_metadata_block = True
                continue
            
            # Handle nested braces or empty lines within metadata blocks
            # This is a simple heuristic to detect the end of a block.
            if in_metadata_block:
                if stripped == '}' or stripped == '': 
                    # potential end of block or empty line inside
                    if stripped == '}':
                        in_metadata_block = False
                    continue
                continue

            # 2. Skip empty lines
            if not stripped:
                continue

            # 3. Extract Title
            if not found_title:
                if stripped.startswith('# '):
                    title = stripped[2:].strip()
                    found_title = True
                continue

            # 4. Extract Description
            # Use the first valid paragraph of text.
            if stripped.startswith('#') or stripped.startswith('<') or stripped.startswith('>'):
                continue

            description = stripped
            break

        if not description:
            description = "No description available."

        return DocumentMetadata(
            filename=file_path.name,
            section=section,
            title=title,
            description=description,
            path=file_path
        )


# --- Skill Generation ---

class SkillGenerator:
    """Orchestrates the creation of the skill directory and artifacts."""

    def __init__(self, config: Configuration, repo: GitRepository):
        self.config = config
        self.repo = repo
        self.parser = ContentParser(repo.path)
        self.metadata_registry: List[DocumentMetadata] = []
        self.version: Optional[str] = None

    def build(self):
        """Main build execution flow."""
        # 1. Analyze Repository
        self.version = self.parser.get_version(self.config.TOC_REL_PATH)
        
        # Only log version found
        if self.version:
            logger.info(f"Found Swift version: {self.version}")
            
        # Export version to GitHub Actions
        gh_output = os.getenv('GITHUB_OUTPUT')
        if gh_output:
            with open(gh_output, 'a') as f:
                ver = self.version or "Unknown"
                f.write(f"swift_version={ver}\n")
        
        doc_order = self.parser.parse_toc_order(self.config.TOC_REL_PATH)
        
        # 2. Prepare Output Directory
        if not self.config.dry_run:
            self._prepare_directory()

        # 3. Process Content
        self._process_files(doc_order)
        
        # Copy License
        if not self.config.dry_run:
            self._copy_license()
            
        # 4. Generate Index
        if not self.config.dry_run:
            self._generate_skill_md()

        # 5. Create Zip Archive
        if not self.config.dry_run:
            self._create_zip_archive()

        logger.info("Packaging complete")

    def _prepare_directory(self):
        """Create clean output directory."""
        if self.config.output_path.exists():
            # No log needed for cleanup unless error occurs
            shutil.rmtree(self.config.output_path)
        
        self.config.output_path.mkdir(parents=True)

    def _create_zip_archive(self):
        """Create a zip archive of the skill directory."""
        zip_path = self.config.output_path.with_suffix('.zip')
        
        # Create a zip file containing the directory content
        shutil.make_archive(
            str(self.config.output_path),
            'zip',
            root_dir=self.config.output_path.parent,
            base_dir=self.config.output_path.name
        )
        logger.info(f"Archive created: {zip_path}")

    def _process_files(self, doc_order: List[str]):
        """Copy files and extract metadata in correct order."""
        
        # Pre-index all available files by name to enable O(1) lookup
        # when iterating through the TOC order.
        # name -> (section, path)
        file_map: Dict[str, Tuple[str, Path]] = {}
        
        for section in self.config.SECTIONS:
            section_path = self.parser.tspl_root / section
            if not section_path.exists():
                # Warn only if section is missing
                logger.warning(f"Missing section: {section}")
                continue
                
            for md_file in section_path.glob("*.md"):
                file_map[md_file.stem] = (section, md_file)

        # Process in TOC order
        processed_sections: Dict[str, int] = {s: 0 for s in self.config.SECTIONS}
        
        for doc_name in doc_order:
            if doc_name not in file_map:
                continue
                
            section, source_path = file_map[doc_name]
            
            # Extract Metadata
            metadata = self.parser.extract_metadata(source_path, section)
            self.metadata_registry.append(metadata)
            processed_sections[section] += 1
            
            # Copy File
            if not self.config.dry_run:
                dest_dir = self.config.output_path / section
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, dest_dir / source_path.name)
                
        # Validate that we found content for all expected sections
        missing_sections = [s for s, count in processed_sections.items() if count == 0]
        if missing_sections:
            raise RuntimeError(f"No documents found for sections: {missing_sections}. Expected structure changed.")

        logger.info(f"Processed {len(self.metadata_registry)} files to {self.config.output_path}")

    def _copy_license(self):
        """
        Copy the license file from the repository.
        
        Required to comply with the Apache 2.0 license, which mandates
        including a copy of the license with any redistribution.
        """
        # Try common license filenames
        for name in ["LICENSE.md", "LICENSE", "LICENSE.txt"]:
            lic_path = self.repo.path / name
            if lic_path.exists():
                shutil.copy2(lic_path, self.config.output_path / name)
                logger.info(f"Included license: {name}")
                return
        
        raise FileNotFoundError("No license file found in repository root. Required for packaging.")

    def _generate_skill_md(self):
        """Generate the SKILL.md index file."""
        
        # Organize by section
        sections: Dict[str, List[DocumentMetadata]] = {s: [] for s in self.config.SECTIONS}
        for meta in self.metadata_registry:
            if meta.section in sections:
                sections[meta.section].append(meta)

        # Frontmatter
        version_suffix = f" ({self.version})" if self.version else ""
        desc = (
            f"Provides the complete content of 'The Swift Programming Language{version_suffix}' book by Apple. "
            "Use this skill when you need to verify Swift syntax, look up language features, understand concurrency, "
            "resolve compiler errors, or consult the formal language reference."
        )
        
        content = [
            "---",
            f"name: {self.config.SKILL_NAME}",
            f"description: {desc}",
            "---",
            "",
            "# The Swift Programming Language",
            "",
            f"The entire content of The Swift Programming Language{version_suffix} book by Apple. This is a comprehensive language reference and guide to the Swift programming language.",
            "",
            "## Documentation Structure",
            ""
        ]

        # Sections
        section_titles = {
            'GuidedTour': 'Getting Started (GuidedTour)',
            'LanguageGuide': 'Language Guide',
            'ReferenceManual': 'Reference Manual'
        }

        for section_key in self.config.SECTIONS:
            docs = sections.get(section_key, [])
            if not docs:
                continue
                
            display_title = section_titles.get(section_key, section_key)
            content.append(f"### {display_title}")
            content.append("")
            
            for doc in docs:
                rel_path = f"{doc.section}/{doc.filename}"
                # Markdown links break if descriptions contain brackets, so we
                # replace them with parentheses for safety.
                safe_desc = doc.description.replace('[', '(').replace(']', ')')
                entry = f"- **{doc.title}** ([{rel_path}]({rel_path})): {safe_desc}"
                content.append(entry)
            
            content.append("")

        # Usage Notes & License
        content.extend([
            "## Usage Notes",
            "",
            "- Organized progressively: GuidedTour → LanguageGuide → ReferenceManual",
            "",
            "## License & Attribution",
            "",
            f"This skill contains content from [The Swift Programming Language]({self.config.REPO_URL}), "
            "distributed under the **Apache 2.0 License**.",
            "",
            "Copyright © Apple Inc. and the Swift project authors.",
            "",
            "This package is a derivative work that aggregates the original markdown content "
            "into a structure optimized for LLM context.",
            ""
        ])

        # Write file
        out_file = self.config.output_path / "SKILL.md"
        out_file.write_text('\n'.join(content), encoding='utf-8')


# --- Entry Point ---

def signal_handler(sig, frame):
    """Handle interrupt signals gracefully."""
    print("\nOperation cancelled by user.", file=sys.stderr)
    sys.exit(0)

def main():
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(
        description="Package The Swift Programming Language into a Skill."
    )
    parser.add_argument(
        "--output", "-o", 
        type=str, 
        help="Output directory (default: ./programming-swift)"
    )
    parser.add_argument(
        "--keep-temp", 
        action="store_true", 
        help="Do not delete temporary repository clone"
    )
    parser.add_argument(
        "--dry-run", 
        action="store_true", 
        help="Simulate operations without writing files"
    )

    args = parser.parse_args()
    config = Configuration.from_args(args)

    try:
        with GitRepository(config.REPO_URL, config.keep_temp) as repo:
            generator = SkillGenerator(config, repo)
            generator.build()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
