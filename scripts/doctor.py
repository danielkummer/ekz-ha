#!/usr/bin/env python3
"""Setup doctor and bootstrap helper for ekz-ha.

Interactive tool that checks prerequisites, validates config, and helps
with first-time setup.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ANSI colors for output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{BOLD}{BLUE}{'=' * 60}{RESET}")
    print(f"{BOLD}{BLUE}{text}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 60}{RESET}\n")


def print_ok(text: str) -> None:
    """Print a success message."""
    print(f"{GREEN}✓{RESET} {text}")


def print_warn(text: str) -> None:
    """Print a warning message."""
    print(f"{YELLOW}⚠{RESET} {text}")


def print_error(text: str) -> None:
    """Print an error message."""
    print(f"{RED}✗{RESET} {text}")


def print_info(text: str) -> None:
    """Print an info message."""
    print(f"{BLUE}ℹ{RESET} {text}")


def ask_yes_no(question: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        response = input(f"{question}{suffix}").strip().lower()
        if not response:
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("Please answer yes or no.")


def check_command(cmd: str, name: str) -> bool:
    """Check if a command is available."""
    if shutil.which(cmd):
        print_ok(f"{name} is installed")
        return True
    else:
        print_error(f"{name} is NOT installed")
        return False


def check_docker() -> bool:
    """Check Docker and Docker Compose."""
    print_header("Checking Docker")
    
    docker_ok = check_command("docker", "Docker")
    compose_ok = check_command("docker", "Docker Compose")
    
    if not docker_ok:
        print_error("Docker is required. Install from https://docs.docker.com/get-docker/")
        return False
    
    # Check if Docker daemon is running
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0:
            print_ok("Docker daemon is running")
        else:
            print_error("Docker daemon is not running")
            return False
    except Exception as e:
        print_error(f"Could not check Docker daemon: {e}")
        return False
    
    # Check Docker Compose
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.decode().strip()
            print_ok(f"Docker Compose is available ({version})")
            return True
        else:
            print_error("Docker Compose is not available")
            print_info("Install with: pip install docker-compose")
            return False
    except Exception as e:
        print_error(f"Could not check Docker Compose: {e}")
        return False


def check_config() -> tuple[bool, Path | None]:
    """Check if config.yaml exists."""
    print_header("Checking Configuration")
    
    config_path = Path("config.yaml")
    example_path = Path("config.yaml.example")
    
    if config_path.exists():
        print_ok(f"config.yaml exists ({config_path.absolute()})")
        return True, config_path
    else:
        print_warn("config.yaml does NOT exist")
        
        if not example_path.exists():
            print_error("config.yaml.example not found either!")
            return False, None
        
        print_info(f"Found example config: {example_path.absolute()}")
        
        if ask_yes_no("Would you like to create config.yaml from example?"):
            try:
                shutil.copy(example_path, config_path)
                print_ok(f"Created {config_path.absolute()}")
                print_warn("⚠ You MUST edit config.yaml and fill in your EKZ credentials!")
                print_info("Required fields: ekz.username, ekz.password, ekz.address")
                return True, config_path
            except Exception as e:
                print_error(f"Could not create config.yaml: {e}")
                return False, None
        else:
            print_info(f"Skipping. Create it manually: cp {example_path} {config_path}")
            return False, None


def validate_config(config_path: Path) -> bool:
    """Validate config using the --check-config command."""
    print_header("Validating Configuration")
    
    if not config_path.exists():
        print_error("config.yaml not found")
        return False
    
    try:
        result = subprocess.run(
            ["python3", "-m", "scraper.main", "--check-config"],
            capture_output=True,
            timeout=10
        )
        
        output = result.stdout.decode()
        error = result.stderr.decode()
        
        if result.returncode == 0:
            print_ok("Configuration is valid!")
            print(f"\n{output}")
            return True
        else:
            print_error("Configuration has errors:")
            print(f"\n{error}")
            return False
    except FileNotFoundError:
        print_warn("Python scraper module not found (normal in Docker-only setup)")
        print_info("Config validation will happen when container starts")
        return True
    except Exception as e:
        print_error(f"Could not validate config: {e}")
        return False


def check_data_dir() -> bool:
    """Check data directory."""
    print_header("Checking Data Directory")
    
    data_dir = Path("data")
    
    if data_dir.exists():
        if data_dir.is_dir():
            print_ok(f"data/ directory exists ({data_dir.absolute()})")
            
            # Check if writable
            test_file = data_dir / ".write_test"
            try:
                test_file.write_text("test")
                test_file.unlink()
                print_ok("data/ directory is writable")
                return True
            except Exception as e:
                print_error(f"data/ directory is NOT writable: {e}")
                return False
        else:
            print_error("data/ exists but is not a directory!")
            return False
    else:
        print_warn("data/ directory does NOT exist")
        
        if ask_yes_no("Would you like to create it?"):
            try:
                data_dir.mkdir(parents=True)
                print_ok(f"Created {data_dir.absolute()}")
                return True
            except Exception as e:
                print_error(f"Could not create data/ directory: {e}")
                return False
        else:
            print_info("Skipping. Create it manually: mkdir data")
            return False


def test_ha_connectivity(config_path: Path) -> bool:
    """Test Home Assistant connectivity if configured."""
    print_header("Testing Home Assistant Connectivity")
    
    if not config_path.exists():
        print_warn("No config.yaml found, skipping HA test")
        return False
    
    try:
        import yaml
        import requests
        
        with open(config_path) as f:
            config = yaml.safe_load(f)
        
        ha_config = config.get("home_assistant", {})
        ha_url = ha_config.get("url")
        ha_token = ha_config.get("token")
        
        if not ha_url or not ha_token:
            print_info("Home Assistant not configured in config.yaml (optional)")
            return True
        
        print_info(f"Testing connectivity to {ha_url}...")
        
        response = requests.get(
            f"{ha_url}/api/",
            headers={"Authorization": f"Bearer {ha_token}"},
            timeout=10
        )
        
        if response.status_code == 200:
            print_ok("Home Assistant is reachable!")
            api_data = response.json()
            print_info(f"  Version: {api_data.get('version', 'unknown')}")
            return True
        elif response.status_code == 401:
            print_error("Home Assistant token is invalid (401 Unauthorized)")
            return False
        else:
            print_warn(f"Home Assistant returned status {response.status_code}")
            return False
    except ImportError:
        print_warn("PyYAML or requests not installed, skipping HA test")
        print_info("Install with: pip install pyyaml requests")
        return True
    except Exception as e:
        print_error(f"Could not test Home Assistant connectivity: {e}")
        return False


def check_ssh_key() -> bool:
    """Check if SSH key exists for rsync."""
    print_header("Checking SSH Key (for rsync)")
    
    ssh_dir = Path("ssh")
    key_path = ssh_dir / "id_ed25519"
    
    if key_path.exists():
        print_ok(f"SSH key exists ({key_path.absolute()})")
        return True
    else:
        print_info("No SSH key found (only needed for rsync)")
        
        if ask_yes_no("Would you like to generate an SSH key for rsync?", default=False):
            try:
                ssh_dir.mkdir(exist_ok=True)
                subprocess.run([
                    "ssh-keygen",
                    "-t", "ed25519",
                    "-f", str(key_path),
                    "-N", "",  # No passphrase
                    "-C", "ekz-ha-rsync"
                ], check=True)
                print_ok(f"Generated SSH key: {key_path}")
                print_info(f"Public key: {key_path}.pub")
                print_warn("Add the public key to your remote server's authorized_keys")
                return True
            except Exception as e:
                print_error(f"Could not generate SSH key: {e}")
                return False
        else:
            print_info("Skipping SSH key generation")
            return True


def show_next_steps(all_ok: bool) -> None:
    """Show next steps."""
    print_header("Next Steps")
    
    if all_ok:
        print_ok("All checks passed! You're ready to go.")
        print()
        print("To start the scraper:")
        print(f"  {BOLD}docker compose up -d{RESET}")
        print()
        print("To view logs:")
        print(f"  {BOLD}docker compose logs -f{RESET}")
        print()
        print("To check status:")
        print(f"  {BOLD}cat data/status.json{RESET}")
    else:
        print_warn("Some checks failed. Review the issues above.")
        print()
        print("Common fixes:")
        print("  - Edit config.yaml and fill in EKZ credentials")
        print("  - Ensure Docker is running: sudo systemctl start docker")
        print("  - Create missing directories: mkdir data")


def main() -> int:
    """Run the setup doctor."""
    print(f"\n{BOLD}ekz-ha Setup Doctor{RESET}")
    print("This tool checks your setup and helps with first-time configuration.\n")
    
    # Change to project root
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    os.chdir(project_root)
    print_info(f"Working directory: {project_root.absolute()}")
    
    checks = []
    
    # Check Docker
    checks.append(("Docker", check_docker()))
    
    # Check config
    config_exists, config_path = check_config()
    checks.append(("Config", config_exists))
    
    # Validate config if it exists
    if config_exists and config_path:
        checks.append(("Config validation", validate_config(config_path)))
        checks.append(("HA connectivity", test_ha_connectivity(config_path)))
    
    # Check data directory
    checks.append(("Data directory", check_data_dir()))
    
    # Check SSH key (optional)
    check_ssh_key()
    
    # Summary
    print_header("Summary")
    
    all_ok = True
    for name, result in checks:
        if result:
            print_ok(f"{name}: OK")
        else:
            print_error(f"{name}: FAILED")
            all_ok = False
    
    show_next_steps(all_ok)
    
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nAborted by user.")
        sys.exit(130)
