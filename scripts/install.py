"""Installation script for the Sanjaya plugin."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
from typing import Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description="Install Sanjaya plugin into Antigravity and/or Jetski."
  )
  parser.add_argument(
      "--target",
      choices=("antigravity", "jetski", "both"),
      default=None,
      help="Target application environment (default: both).",
  )
  parser.add_argument(
      "--workspace",
      type=str,
      default=None,
      help="Install to a workspace at PATH/.agents/plugins/sanjaya.",
  )
  parser.add_argument(
      "--copy",
      action="store_true",
      default=False,
      help="Copy files instead of creating a symlink.",
  )
  parser.add_argument(
      "--force",
      action="store_true",
      default=False,
      help="Replace existing files, symlinks, or directories.",
  )
  parser.add_argument(
      "--uninstall",
      action="store_true",
      default=False,
      help="Uninstall the plugin from target directories.",
  )
  parser.add_argument(
      "--dry-run",
      action="store_true",
      default=False,
      help="Print actions without modifying the filesystem.",
  )
  return parser


def resolve_destinations(
    target_choice: str | None, workspace_arg: str | None
) -> list[pathlib.Path]:
  destinations: list[pathlib.Path] = []
  home_dir = pathlib.Path.home()
  antigravity_dest = home_dir / ".gemini" / "antigravity" / "plugins" / "sanjaya"
  jetski_dest = home_dir / ".gemini" / "config" / "plugins" / "sanjaya"

  if workspace_arg:
    workspace_path = pathlib.Path(workspace_arg).expanduser().resolve()
    destinations.append(workspace_path / ".agents" / "plugins" / "sanjaya")

  if workspace_arg and target_choice is None:
    return destinations

  selected_target = target_choice or "both"
  if selected_target in ("antigravity", "both"):
    destinations.append(antigravity_dest)
  if selected_target in ("jetski", "both"):
    destinations.append(jetski_dest)
  return destinations


def perform_uninstall(
    dest_path: pathlib.Path, force_mode: bool, dry_run: bool
) -> int:
  if dest_path.is_symlink():
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove symlink: {dest_path}\n")
    else:
      dest_path.unlink()
      sys.stdout.write(f"Removed symlink: {dest_path}\n")
    return 0

  if dest_path.is_dir():
    if not force_mode:
      sys.stderr.write(
          f"Conflict: {dest_path} is a directory, not a symlink. "
          "Use --force to remove.\n"
      )
      return 1
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove directory: {dest_path}\n")
    else:
      shutil.rmtree(dest_path)
      sys.stdout.write(f"Removed directory: {dest_path}\n")
    return 0

  if dest_path.is_file():
    if not force_mode:
      sys.stderr.write(
          f"Conflict: {dest_path} is a file, not a symlink. "
          "Use --force to remove.\n"
      )
      return 1
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove file: {dest_path}\n")
    else:
      dest_path.unlink()
      sys.stdout.write(f"Removed file: {dest_path}\n")
    return 0

  sys.stdout.write(f"Not installed (does not exist): {dest_path}\n")
  return 0


def prepare_target_slot(
    dest_path: pathlib.Path, copy_mode: bool, force_mode: bool, dry_run: bool
) -> tuple[int, bool]:
  if dest_path.is_symlink():
    target_link = dest_path.resolve()
    if not copy_mode and target_link == REPO_ROOT.resolve():
      sys.stdout.write(
          f"Already installed: {dest_path} points to {REPO_ROOT.resolve()}\n"
      )
      return 0, True
    if not force_mode:
      sys.stderr.write(
          f"Conflict: symlink exists at {dest_path} pointing to {target_link}. "
          "Use --force to replace.\n"
      )
      return 1, False
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove existing symlink: {dest_path}\n")
    else:
      dest_path.unlink()
    return 0, False

  if dest_path.is_dir():
    if not force_mode:
      sys.stderr.write(
          f"Conflict: directory already exists at {dest_path}. "
          "Use --force to replace.\n"
      )
      return 1, False
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove existing directory: {dest_path}\n")
    else:
      shutil.rmtree(dest_path)
    return 0, False

  if dest_path.is_file():
    if not force_mode:
      sys.stderr.write(
          f"Conflict: file already exists at {dest_path}. "
          "Use --force to replace.\n"
      )
      return 1, False
    if dry_run:
      sys.stdout.write(f"[dry-run] Would remove existing file: {dest_path}\n")
    else:
      dest_path.unlink()
    return 0, False

  return 0, False


def perform_install(
    dest_path: pathlib.Path, copy_mode: bool, force_mode: bool, dry_run: bool
) -> int:
  slot_exit_code, already_installed = prepare_target_slot(
      dest_path, copy_mode, force_mode, dry_run
  )
  if slot_exit_code != 0:
    return slot_exit_code
  if already_installed:
    return 0

  if copy_mode:
    if dry_run:
      sys.stdout.write(
          f"[dry-run] Would copy plugin {REPO_ROOT} -> {dest_path}\n"
      )
      return 0
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    ignore_pattern = shutil.ignore_patterns(
        ".git*", "__pycache__", "tests", ".github", ".venv", "*.pyc", "*.tmp*"
    )
    shutil.copytree(REPO_ROOT, dest_path, ignore=ignore_pattern)
    sys.stdout.write(f"Copied plugin {REPO_ROOT} -> {dest_path}\n")
    return 0

  if dry_run:
    sys.stdout.write(
        f"[dry-run] Would create symlink: {dest_path} -> {REPO_ROOT.resolve()}\n"
    )
    return 0
  dest_path.parent.mkdir(parents=True, exist_ok=True)
  dest_path.symlink_to(REPO_ROOT.resolve())
  sys.stdout.write(f"Created symlink: {dest_path} -> {REPO_ROOT.resolve()}\n")
  return 0


def main(argv: Sequence[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  destinations = resolve_destinations(args.target, args.workspace)

  for dest_path in destinations:
    if args.uninstall:
      uninstall_status = perform_uninstall(
          dest_path, args.force, args.dry_run
      )
      if uninstall_status != 0:
        return uninstall_status
    else:
      install_status = perform_install(
          dest_path, args.copy, args.force, args.dry_run
      )
      if install_status != 0:
        return install_status
  return 0


if __name__ == "__main__":
  sys.exit(main())
