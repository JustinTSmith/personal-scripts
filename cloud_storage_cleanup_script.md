# cloud_storage_cleanup_script.py

## What it is

A duplicate file scanner and mover for Dropbox and Google Drive local sync folders.

## What it does

Scans both Dropbox and Google Drive directories for duplicate files, identified by matching filename and file size. When duplicates are found:

1. Keeps one copy in its original location
2. Moves the duplicate(s) to a `/cleanup` subdirectory within the same cloud storage root

The script does **not** delete files — it moves them to `/cleanup` so you can review before permanently removing anything.

## How to run

```bash
python3 cloud_storage_cleanup_script.py
```

By default it scans the standard macOS paths for Dropbox (`~/Dropbox`) and Google Drive (`~/Google Drive`). Adjust paths at the top of the script if your sync folders are in a different location.

## Notes

- Duplicate detection is by name + size only, not content hash — near-identical files with different sizes won't be flagged
- Review the `/cleanup` folder before deleting to avoid losing anything important

## Dependencies

- Python 3 (standard library only — no pip installs required)
- Dropbox and/or Google Drive syncing locally
