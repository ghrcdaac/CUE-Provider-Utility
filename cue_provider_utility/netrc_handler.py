import netrc
import os
from pathlib import Path
import logging
from typing import Optional

logger = logging.getLogger(__name__)
NETRC_PATH = Path.home() / ".netrc"

def get_key_from_netrc(hostname: str) -> Optional[str]:
    """Retrieves API key (password) for a given hostname from the .netrc file."""
    if not NETRC_PATH.exists():
        return None
    
    try:
        netrc_data = netrc.netrc(str(NETRC_PATH))
        auth = netrc_data.authenticators(hostname)
        if auth:
            # .authenticators returns (login, account, password)
            key = auth[2]
            logger.debug(f"API key found in .netrc for host {hostname}.")
            return key
    except (FileNotFoundError, netrc.NetrcParseError, AttributeError) as e:
        logger.warning(f"Could not read or parse .netrc file at {NETRC_PATH}: {e}")
    return None

def save_key_to_netrc(hostname: str, login: str, key: str) -> None:
    """Saves or updates an API key in the .netrc file for a given hostname."""
    lines = []
    entry_found = False

    if NETRC_PATH.exists():
        with open(NETRC_PATH, 'r') as f:
            lines = f.readlines()

    # Rewrite the file, replacing or adding the entry
    with open(NETRC_PATH, 'w') as f:
        in_entry_block = False
        for line in lines:
            parts = line.strip().split()
            # If we find the start of the machine entry we are interested in
            if not in_entry_block and parts and parts[0] == 'machine' and parts[1] == hostname:
                in_entry_block = True
                entry_found = True
                # Skip writing this line and its subsequent password/login lines
                continue
            # If we find the start of a new machine, we are no longer in our target block
            elif in_entry_block and parts and parts[0] == 'machine':
                in_entry_block = False
            
            if not in_entry_block:
                f.write(line)

        # Append the new or updated entry at the end
        if lines and not lines[-1].endswith('\n'):
            f.write('\n')
        f.write(f"machine {hostname}\n")
        f.write(f"    login {login}\n")
        f.write(f"    password {key}\n")

    os.chmod(NETRC_PATH, 0o600)
    logger.info(f"API key for host {hostname} saved to .netrc.")

def remove_key_from_netrc(hostname: str) -> bool:
    """Removes a machine entry entirely from the .netrc file."""
    if not NETRC_PATH.exists():
        return False

    lines = []
    entry_was_found = False
    with open(NETRC_PATH, 'r') as f:
        lines = f.readlines()
    
    # Rewrite the file, excluding the specified machine entry
    with open(NETRC_PATH, 'w') as f:
        in_block_to_remove = False
        for line in lines:
            parts = line.strip().split()
            if not in_block_to_remove and parts and parts[0] == 'machine' and parts[1] == hostname:
                in_block_to_remove = True
                entry_was_found = True
                continue
            elif in_block_to_remove and parts and parts[0] == 'machine':
                in_block_to_remove = False
            
            if not in_block_to_remove:
                f.write(line)
    
    if entry_was_found:
        logger.info(f"API key for host {hostname} removed from .netrc.")
    return entry_was_found
