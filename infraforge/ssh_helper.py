"""SSH helper utilities for template export/import operations.

Provides functions to test SSH connectivity, find/generate SSH keys,
install sshpass, and copy SSH keys to remote hosts. All functions are
standalone with no Textual dependencies — stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# Preferred SSH key types in order of preference.
_KEY_TYPES = ["ed25519", "rsa", "ecdsa", "dsa"]


def test_ssh(host: str, user: str = "root", timeout: int = 5) -> bool:
    """Test if SSH key-based auth works to the given host.

    Uses BatchMode to ensure no interactive password prompt.
    Accepts new host keys automatically via StrictHostKeyChecking=accept-new.

    Returns True if the SSH connection succeeds, False otherwise.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        f"{user}@{host}",
        "echo", "ok",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # generous grace period beyond SSH's own timeout
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        # ssh binary not found or other OS-level error
        return False
    except Exception:
        return False


def find_ssh_keys() -> list[Path]:
    """Find available SSH public keys in ~/.ssh/.

    Returns a list of Path objects for keys that exist, ordered by
    preference: ed25519, rsa, ecdsa, dsa.
    """
    ssh_dir = Path.home() / ".ssh"
    keys: list[Path] = []
    for key_type in _KEY_TYPES:
        pub_key = ssh_dir / f"id_{key_type}.pub"
        if pub_key.is_file():
            keys.append(pub_key)
    return keys


def ensure_sshpass() -> tuple[bool, str]:
    """Ensure sshpass is installed, installing it if necessary.

    Returns (success, message) where message describes what happened:
    - "already installed" if sshpass was found on PATH
    - "installed successfully" if apt-get install succeeded
    - error description if installation failed
    """
    # Check if already available
    if shutil.which("sshpass") is not None:
        return True, "already installed"

    # Attempt to install via apt-get with passwordless sudo
    try:
        result = subprocess.run(
            ["sudo", "-n", "apt-get", "install", "-y", "sshpass"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            # Verify it's now available
            if shutil.which("sshpass") is not None:
                return True, "installed successfully"
            return False, "apt-get reported success but sshpass not found on PATH"
        # Installation failed — extract useful error info
        stderr = result.stderr.strip()
        if stderr:
            # Return last line for brevity (apt errors are verbose)
            last_line = stderr.splitlines()[-1]
            return False, f"Failed to install sshpass: {last_line}"
        return False, "Failed to install sshpass (apt-get returned non-zero)"
    except subprocess.TimeoutExpired:
        return False, "Timed out installing sshpass"
    except FileNotFoundError:
        return False, "sudo or apt-get not found — cannot install sshpass automatically"
    except OSError as exc:
        return False, f"OS error installing sshpass: {exc}"
    except Exception as exc:
        return False, f"Unexpected error installing sshpass: {exc}"


def copy_ssh_key(
    host: str,
    password: str,
    user: str = "root",
    key_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """Copy an SSH public key to the remote host using password auth.

    Uses sshpass + ssh-copy-id to push the public key. If key_path is
    None, the first key from find_ssh_keys() is used.

    Returns (success, message) describing the outcome.
    """
    # Resolve which key to copy
    if key_path is None:
        keys = find_ssh_keys()
        if not keys:
            return False, "No SSH public keys found in ~/.ssh/"
        key_path = keys[0]
    elif not key_path.is_file():
        return False, f"SSH public key not found: {key_path}"

    # Ensure sshpass is available
    ok, msg = ensure_sshpass()
    if not ok:
        return False, msg

    # Copy the key using sshpass + ssh-copy-id
    cmd = [
        "sshpass", "-p", password,
        "ssh-copy-id",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", str(key_path),
        f"{user}@{host}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out copying SSH key (30s)"
    except FileNotFoundError:
        return False, "sshpass or ssh-copy-id not found on PATH"
    except OSError as exc:
        return False, f"OS error copying SSH key: {exc}"
    except Exception as exc:
        return False, f"Unexpected error copying SSH key: {exc}"

    if result.returncode == 0:
        # Verify that key-based auth now works
        if test_ssh(host, user=user):
            return True, "SSH key auth configured successfully"
        return False, "Key copied but verification failed"

    # Parse stderr for actionable messages
    stderr = result.stderr.strip()
    if "ermission denied" in stderr:
        return False, "Incorrect password"
    return False, f"SSH key copy failed: {stderr}" if stderr else "SSH key copy failed"


def generate_ssh_key(
    key_type: str = "ed25519",
) -> tuple[bool, str, Optional[Path]]:
    """Generate a new SSH keypair if none exist.

    If keys already exist, returns the first one without generating.
    Otherwise creates ~/.ssh/ (mode 0700) and runs ssh-keygen.

    Returns (success, message, path_to_pub_key_or_None).
    """
    # Check for existing keys first
    existing = find_ssh_keys()
    if existing:
        return True, "SSH key already exists", existing[0]

    ssh_dir = Path.home() / ".ssh"
    key_file = ssh_dir / f"id_{key_type}"
    pub_file = ssh_dir / f"id_{key_type}.pub"

    # Create ~/.ssh/ if it doesn't exist
    try:
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Ensure correct permissions even if directory already existed
        os.chmod(str(ssh_dir), 0o700)
    except OSError as exc:
        return False, f"Failed to create {ssh_dir}: {exc}", None

    # Generate the keypair
    cmd = [
        "ssh-keygen",
        "-t", key_type,
        "-N", "",
        "-f", str(key_file),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out generating SSH key (30s)", None
    except FileNotFoundError:
        return False, "ssh-keygen not found on PATH", None
    except OSError as exc:
        return False, f"OS error generating SSH key: {exc}", None
    except Exception as exc:
        return False, f"Unexpected error generating SSH key: {exc}", None

    if result.returncode == 0 and pub_file.is_file():
        return True, f"Generated {key_type} SSH keypair at {key_file}", pub_file

    stderr = result.stderr.strip()
    if stderr:
        return False, f"ssh-keygen failed: {stderr}", None
    return False, "ssh-keygen failed (unknown error)", None
