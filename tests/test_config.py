from pathlib import Path

import pytest

from antigrafa.config import Config, ConfigError, SourceConfig, Tuning, load_config
from antigrafa.platforms.proxmox import ProxmoxSettings
from antigrafa.platforms.proxmox import SSHConfig as ProxmoxSSH

EXAMPLE = Path(__file__).resolve().parents[1] / "antigrafa.example.toml"

MINIMAL = """
[target.local]
path = "/srv/antigrafa"
"""

PROXMOX = """
[source.pve]
type = "proxmox"
url = "https://pve"
token_id = "a@pve!b"
"""


def _write(tmp_path, text):
    path = tmp_path / "antigrafa.toml"
    path.write_text(text)
    return path


def test_example_configuration_is_valid():
    cfg = load_config(EXAMPLE)
    source = cfg.source("cluster1")
    assert source.type == "proxmox" and source.name == "cluster1"
    pve = source.settings
    assert pve.url == "https://pve1.example.lan:8006"
    assert pve.ca_file == "/etc/antigrafa/pve-root-ca.pem"
    assert pve.ssh.identity_file == "/etc/antigrafa/id_ed25519"
    assert set(cfg.targets) == {"local", "nas"}
    assert cfg.targets["nas"].kind == "nfs"
    assert cfg.targets["nas"].name == "nas"
    assert cfg.tuning == Tuning()
    assert pve.node_host("pve1") == "10.0.0.11"
    assert pve.node_host("pve9") == "pve9"  # unknown node: name used as is


def test_minimal_configuration_uses_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.sources == {}
    assert cfg.tuning == Tuning()
    assert cfg.targets["local"].sync == "filesystem"


def test_proxmox_source_defaults(tmp_path):
    settings = load_config(_write(tmp_path, MINIMAL + PROXMOX)).source("pve").settings
    assert isinstance(settings, ProxmoxSettings)
    assert settings.ssh == ProxmoxSSH()
    assert settings.nodes == {}


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="antigrafa.toml"):
        load_config(_write(tmp_path, "[target.local\n"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINIMAL + "[other]\n", "unknown sections: other"),
        ("[tuning]\nchunk_size_mib = 4\n", "at least one"),
        (MINIMAL + "[tuning]\nchunk_size = 4\n", r"\[tuning\]: unknown keys chunk_size"),
        ("[target.x]\npath = '/x'\nkind = 'smb'\n", r"\[target.x\]: target x: unknown kind 'smb'"),
        ("[target.x]\nkind = 'local'\n", r"\[target.x\]: .*path"),
        (MINIMAL + "[source.pve]\ntype = 'proxmox'\nurl = 'https://pve'\n", r"\[source.pve\]: .*token_id"),
        # Sources
        (MINIMAL + "[source.pve]\nurl = 'https://pve'\n", r"\[source.pve\]: type is required \(proxmox\)"),
        (MINIMAL + "[source.pve]\ntype = 'esx'\n", r"\[source.pve\]: unknown platform type 'esx' \(supported: proxmox\)"),
        (MINIMAL + "[source.'../x']\ntype = 'proxmox'\n", r"invalid source name '../x'"),
        ("source = 3\n" + MINIMAL, r"\[source\] must be a table"),
        ("target = 'x'\n", r"\[target\] must be a table"),
        (MINIMAL + PROXMOX + "color = 'red'\n", r"\[source.pve\]: unknown keys color"),
        (MINIMAL + PROXMOX + "[source.pve.ssh]\nport = 22\nuser_name = 'x'\n", r"\[source.pve.ssh\]: unknown keys user_name"),
        (MINIMAL + PROXMOX + "[source.pve.nodes.pve1]\naddress = 'x'\n", r"\[source.pve.nodes.pve1\]: unknown keys address"),
        (MINIMAL + PROXMOX + "nodes = 'pve1'\n", r"\[source.pve.nodes\] must be a table"),
        (MINIMAL + PROXMOX + "ssh = 'root'\n", r"\[source.pve.ssh\] must be a table"),
    ],
)
def test_invalid_content(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize("section", ["proxmox", "ssh", "nodes"])
def test_old_proxmox_only_format_points_to_source(tmp_path, section):
    text = MINIMAL + f"[{section}]\n"
    with pytest.raises(ConfigError, match=r'moved into \[source.<name>\] with type = "proxmox"'):
        load_config(_write(tmp_path, text))


def test_verify_tls_can_be_disabled(tmp_path):
    text = MINIMAL + PROXMOX + "verify_tls = false\n"
    assert load_config(_write(tmp_path, text)).source("pve").settings.verify_tls is False
    # Verified by default
    assert load_config(_write(tmp_path, MINIMAL + PROXMOX)).source("pve").settings.verify_tls is True


def test_verify_tls_false_with_ca_file_is_contradictory(tmp_path):
    text = MINIMAL + PROXMOX + "verify_tls = false\nca_file = '/etc/antigrafa/ca.pem'\n"
    with pytest.raises(ConfigError, match=r"\[source.pve\]: ca_file and verify_tls = false"):
        load_config(_write(tmp_path, text))
    # verify_tls = true with a CA is the normal case
    text = MINIMAL + PROXMOX + "verify_tls = true\nca_file = '/etc/antigrafa/ca.pem'\n"
    assert load_config(_write(tmp_path, text)).source("pve").settings.ca_file == "/etc/antigrafa/ca.pem"


@pytest.mark.parametrize("value", ["'no'", "0"])
def test_verify_tls_must_be_a_boolean(tmp_path, value):
    # "no" or 0 would otherwise silently mean "verify" or "do not verify"
    text = MINIMAL + PROXMOX + f"verify_tls = {value}\n"
    with pytest.raises(ConfigError, match="verify_tls must be true or false"):
        load_config(_write(tmp_path, text))


def test_secret_from_value_or_file(tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file\n")
    text = MINIMAL + PROXMOX + f"token_secret_file = '{secret_file.as_posix()}'\n"
    assert load_config(_write(tmp_path, text)).source("pve").settings.secret() == "from-file"

    text = MINIMAL + PROXMOX + "token_secret = 'inline'\n"
    assert load_config(_write(tmp_path, text)).source("pve").settings.secret() == "inline"


def test_missing_secret(tmp_path):
    with pytest.raises(ConfigError, match=r"\[source.pve\]: token_secret"):
        load_config(_write(tmp_path, MINIMAL + PROXMOX)).source("pve").settings.secret()


def test_target_selection():
    cfg = load_config(EXAMPLE)
    assert cfg.target("nas").path == "/mnt/nas/antigrafa"
    with pytest.raises(ConfigError, match="several targets.*local, nas"):
        cfg.target(None)
    with pytest.raises(ConfigError, match="unknown target: usb"):
        cfg.target("usb")

    single = Config({}, {"local": cfg.targets["local"]}, Tuning())
    assert single.target(None) is cfg.targets["local"]


def test_source_selection():
    a = SourceConfig("a", "proxmox", None)
    b = SourceConfig("b", "proxmox", None)
    assert Config({"a": a}, {}, Tuning()).source(None) is a
    both = Config({"a": a, "b": b}, {}, Tuning())
    assert both.source("b") is b
    with pytest.raises(ConfigError, match="several sources configured, use --source"):
        both.source(None)
    with pytest.raises(ConfigError, match="unknown source: c"):
        both.source("c")


def test_remote_hash_setting(tmp_path):
    assert load_config(_write(tmp_path, MINIMAL + PROXMOX)).source("pve").settings.remote_hash is True
    text = MINIMAL + PROXMOX + "remote_hash = false\n"
    assert load_config(_write(tmp_path, text)).source("pve").settings.remote_hash is False
    with pytest.raises(ConfigError, match="remote_hash must be true or false"):
        load_config(_write(tmp_path, MINIMAL + PROXMOX + "remote_hash = 'yes'\n"))
