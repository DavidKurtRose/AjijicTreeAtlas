#!/usr/bin/env python3
"""
Sync Atlas Media to GitHub
==========================
Compares local files to those already at GitHub and uploads any files
that are new OR have changed, all in a single commit. Unchanged files
are skipped. No matter how many files, it's one commit.

"Changed" is detected by content, not by date modified: the script
computes each local file's Git blob SHA-1 and compares it to the SHA
already stored in the repository. If they differ, the file is re-uploaded.

Usage:
    python sync_github.py

The script will ask you for:
  1. Your GitHub Personal Access Token
  2. Your repository (e.g., DavidKurtRose/AjijicTreeAtlas)
  3. The local folder containing your media files

First-time setup — creating a Personal Access Token:
  1. Go to https://github.com/settings/tokens
  2. Click "Generate new token" → "Generate new token (classic)"
  3. Give it a name like "Atlas Sync"
  4. Check the "repo" scope (full control of private repositories)
  5. Click "Generate token" at the bottom
  6. Copy the token — you won't see it again!

The token is remembered: if a file called .github_token exists in this
folder it's read automatically, otherwise the script prompts for it and
offers to save it there for next time.

The repository is remembered too: the first time you run it, it asks for
your repository URL and saves it to a file called .github_url in this
folder. After that it reads the repository from .github_url automatically.
"""

import os
import sys
import json
import base64
import hashlib
import urllib.request
import urllib.error
from pathlib import Path

# Media extensions the atlas builder recognizes
MEDIA_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg',  # images
    '.mp4', '.webm', '.mov',                                     # video
    '.mp3', '.m4a', '.ogg', '.wav',                              # audio
    '.txt', '.html', '.htm',                                      # text/html
}


def api_request(url, token, method='GET', data=None):
    """Make a GitHub API request and return parsed JSON."""
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'AtlasSync/1.0',
    }
    if data is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(data).encode('utf-8')
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        print(f"\n  API error {e.code}: {error_body[:300]}")
        raise


def git_blob_sha(filepath):
    """Compute the Git blob SHA-1 for a file, matching the value GitHub
    stores for each blob. Reads the file in chunks so large media files
    don't all sit in memory at once."""
    filepath = Path(filepath)
    size = filepath.stat().st_size
    h = hashlib.sha1()
    h.update(f'blob {size}\0'.encode('utf-8'))
    with open(filepath, 'rb') as fp:
        for chunk in iter(lambda: fp.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def get_token():
    """Get GitHub token from file or user input."""
    token_file = Path(__file__).parent / '.github_token'
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            print(f"  Using token from {token_file}")
            return token

    print("  Enter your GitHub Personal Access Token.")
    print("  (It will not be displayed as you type.)")
    print()
    try:
        import getpass
        token = getpass.getpass("  Token: ").strip()
    except Exception:
        token = input("  Token: ").strip()

    if not token:
        print("  No token provided. Exiting.")
        sys.exit(1)

    save = input("  Save token for future use? (y/n): ").strip().lower()
    if save == 'y':
        token_file.write_text(token)
        print(f"  Token saved to {token_file}")

    return token


def normalize_repo(value):
    """Accept either 'owner/repo' or a full GitHub URL (https or SSH) and
    return the canonical 'owner/repo' form."""
    value = value.strip().strip('"').strip("'")
    if value.endswith('.git'):
        value = value[:-4]
    value = value.rstrip('/')
    if value.startswith('git@'):
        # git@github.com:owner/repo
        value = value.split(':', 1)[-1]
    elif 'github.com/' in value:
        value = value.split('github.com/', 1)[1]
    return value


def get_repo(token):
    """Get the repository (owner/repo) from .github_url if present, otherwise
    ask the user and save it to .github_url for next time."""
    url_file = Path(__file__).parent / '.github_url'
    if url_file.exists():
        raw = url_file.read_text().strip()
        if raw:
            repo = normalize_repo(raw)
            if '/' in repo:
                print(f"  Using repository from {url_file}: {repo}")
                return repo

    print()
    raw = input("  Repository URL or owner/repo\n"
                "  (e.g., https://github.com/DavidKurtRose/AjijicTreeAtlas): ").strip()
    repo = normalize_repo(raw)
    if '/' not in repo:
        print("  Repository must be a GitHub URL or in the format owner/repo-name")
        sys.exit(1)

    url_file.write_text(f'https://github.com/{repo}\n')
    print(f"  Saved repository to {url_file}")
    return repo


def collect_media_files(folder):
    """Collect all media files from the folder (not recursive into subfolders
    unless they match atlas naming conventions)."""
    folder = Path(folder)
    files = []
    for item in sorted(folder.rglob('*')):
        if item.is_file() and item.suffix.lower() in MEDIA_EXTS:
            # Skip hidden files and OS metadata
            if any(part.startswith('.') for part in item.relative_to(folder).parts):
                continue
            files.append(item)
    return files


def get_existing_files(token, repo, tree_sha):
    """Return a dict mapping file path -> blob SHA for everything already
    in the repository tree."""
    base = 'https://api.github.com'
    # recursive=1 returns all files in the tree, not just top-level
    tree_data = api_request(
        f'{base}/repos/{repo}/git/trees/{tree_sha}?recursive=1', token
    )
    existing = {}
    for item in tree_data.get('tree', []):
        if item['type'] == 'blob':
            existing[item['path']] = item['sha']
    return existing


def upload_files(token, repo, folder):
    """Sync all media files to the GitHub repo in a single commit, uploading
    files that are new or whose content has changed."""
    base = 'https://api.github.com'
    folder = Path(folder)

    # 1. Collect files
    print("\n  Scanning folder for media files...")
    files = collect_media_files(folder)
    if not files:
        print("  No media files found. Nothing to sync.")
        return

    print(f"  Found {len(files)} media files in local folder.")

    # 2. Get the current HEAD commit and its tree
    print("  Getting repository info...")
    try:
        ref_data = api_request(f'{base}/repos/{repo}/git/ref/heads/main', token)
    except urllib.error.HTTPError:
        print("  Could not find 'main' branch. Trying 'master'...")
        ref_data = api_request(f'{base}/repos/{repo}/git/ref/heads/master', token)

    head_sha = ref_data['object']['sha']
    commit_data = api_request(f'{base}/repos/{repo}/git/commits/{head_sha}', token)
    base_tree_sha = commit_data['tree']['sha']
    print(f"  Current HEAD: {head_sha[:8]}")

    # 3. Check what's already in the repo (path -> blob SHA)
    print("  Checking existing files in repository...")
    existing = get_existing_files(token, repo, base_tree_sha)
    print(f"  Found {len(existing)} files already in repository.")

    # 4. Compare by content: new files and changed files need uploading
    print("  Comparing local files to repository (by content)...")
    new_files = []
    changed_files = []
    unchanged = []
    for f in files:
        rel_path = f.relative_to(folder).as_posix()
        local_sha = git_blob_sha(f)
        repo_sha = existing.get(rel_path)
        if repo_sha is None:
            new_files.append(f)
        elif repo_sha != local_sha:
            changed_files.append(f)
        else:
            unchanged.append(rel_path)

    if unchanged:
        print(f"  {len(unchanged)} files unchanged — skipping.")
    if new_files:
        print(f"  {len(new_files)} new files.")
    if changed_files:
        print(f"  {len(changed_files)} changed files.")

    to_upload = new_files + changed_files
    if not to_upload:
        print("\n  ✓ Everything is already up to date. Nothing to do.")
        return

    total_size = sum(f.stat().st_size for f in to_upload)
    print(f"  {len(to_upload)} files to upload ({total_size / (1024*1024):.1f} MB)")
    print()

    # 5. Create blobs for each file to upload
    print(f"  Uploading {len(to_upload)} files...")
    tree_items = []
    for i, filepath in enumerate(to_upload, 1):
        rel_path = filepath.relative_to(folder).as_posix()
        file_bytes = filepath.read_bytes()
        b64_content = base64.b64encode(file_bytes).decode('ascii')

        size_kb = len(file_bytes) / 1024
        tag = 'changed' if filepath in changed_files else 'new'
        print(f"  [{i}/{len(to_upload)}] {rel_path} ({size_kb:.0f} KB, {tag})",
              end='', flush=True)

        blob_data = api_request(
            f'{base}/repos/{repo}/git/blobs',
            token,
            method='POST',
            data={
                'content': b64_content,
                'encoding': 'base64'
            }
        )
        blob_sha = blob_data['sha']
        print(f" ✓")

        tree_items.append({
            'path': rel_path,
            'mode': '100644',
            'type': 'blob',
            'sha': blob_sha
        })

    # 6. Create a new tree
    print("\n  Creating tree...")
    tree_data = api_request(
        f'{base}/repos/{repo}/git/trees',
        token,
        method='POST',
        data={
            'base_tree': base_tree_sha,
            'tree': tree_items
        }
    )
    new_tree_sha = tree_data['sha']

    # 7. Create a new commit
    print("  Creating commit...")
    n_new = len(new_files)
    n_changed = len(changed_files)
    parts = []
    if n_new:
        parts.append(f'{n_new} new')
    if n_changed:
        parts.append(f'{n_changed} changed')
    summary = ' and '.join(parts)
    new_commit = api_request(
        f'{base}/repos/{repo}/git/commits',
        token,
        method='POST',
        data={
            'message': f'Sync atlas media: {summary} file(s)',
            'tree': new_tree_sha,
            'parents': [head_sha]
        }
    )
    new_commit_sha = new_commit['sha']

    # 8. Update the branch reference
    print("  Updating branch...")
    branch = 'main'
    try:
        api_request(
            f'{base}/repos/{repo}/git/refs/heads/{branch}',
            token,
            method='PATCH',
            data={'sha': new_commit_sha}
        )
    except urllib.error.HTTPError:
        branch = 'master'
        api_request(
            f'{base}/repos/{repo}/git/refs/heads/{branch}',
            token,
            method='PATCH',
            data={'sha': new_commit_sha}
        )

    print(f"\n  ✓ Done! {summary} file(s) uploaded in a single commit.")
    print(f"  Commit: {new_commit_sha[:8]}")
    print(f"  Repository: https://github.com/{repo}")
    print(f"  GitHub Pages URL: https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/")


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Sync Atlas Media to GitHub         ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    # Get token
    token = get_token()

    # Verify token works
    print("\n  Verifying token...")
    try:
        user_data = api_request('https://api.github.com/user', token)
        print(f"  Authenticated as: {user_data['login']}")
    except Exception:
        print("  Token is invalid or expired. Please check and try again.")
        sys.exit(1)

    # Get repo (from .github_url or by asking, saving for next time)
    repo = get_repo(token)

    # Verify repo exists
    try:
        api_request(f'https://api.github.com/repos/{repo}', token)
        print(f"  Repository found: {repo}")
    except Exception:
        print(f"  Repository '{repo}' not found or not accessible.")
        sys.exit(1)

    # Get folder (defaults to the folder this script lives in)
    print()
    script_dir = Path(__file__).parent.resolve()
    folder = input(f"  Local folder containing media files\n"
                   f"  [press Enter for {script_dir}]: ").strip()
    folder = folder.strip('"').strip("'")  # Remove quotes if user wraps path
    if not folder:
        folder = str(script_dir)
    if not os.path.isdir(folder):
        print(f"  Folder not found: {folder}")
        sys.exit(1)

    # Confirm
    files = collect_media_files(folder)
    print(f"\n  Ready to sync {len(files)} media files")
    print(f"  from: {os.path.abspath(folder)}")
    print(f"  to:   https://github.com/{repo}")
    confirm = input("\n  Proceed? (y/n): ").strip().lower()
    if confirm != 'y':
        print("  Cancelled.")
        sys.exit(0)

    upload_files(token, repo, folder)


if __name__ == '__main__':
    main()
