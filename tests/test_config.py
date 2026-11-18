from pathlib import Path

import pytest

from antigrafa.config import Config, ConfigError, SSHConfig, Tuning, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "antigrafa.example.toml"

MINIMAL = """
[target.local]
path = "/srv/antigrafa"
"""


def _write(tmp_path, text):
    path = tmp_path / "antigrafa.toml"
    path.write_text(text)
    return path


def test_example_configuration_is_valid():
    cfg = load_config(EXAMPLE)
    assert cfg.proxmox.url == "https://pve1.example.lan:8006"
    assert cfg.proxmox.ca_file == "/etc/antigrafa/pve-root-ca.pem"
    assert cfg.ssh.identity_file == "/etc/antigrafa/id_ed25519"
    assert set(cfg.targets) == {"local", "nas"}
    assert cfg.targets["nas"].kind == "nfs"
    assert cfg.targets["nas"].name == "nas"
    assert cfg.tuning == Tuning()
    assert cfg.node_host("pve1") == "10.0.0.11"
    assert cfg.node_host("pve9") == "pve9"  # unknown node: name used as is


def test_minimal_configuration_uses_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.proxmox is None
    assert cfg.ssh == SSHConfig()
    assert cfg.nodes == {}
    assert cfg.tuning == Tuning()
    assert cfg.targets["local"].sync == "filesystem"


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
        ("[ssh]\nuser = 'root'\n", "at least one"),
        (MINIMAL + "[tuning]\nchunk_size = 4\n", r"\[tuning\]: unknown keys chunk_size"),
        ("[target.x]\npath = '/x'\nkind = 'smb'\n", r"\[target.x\]: target x: unknown kind 'smb'"),
        ("[target.x]\nkind = 'local'\n", r"\[target.x\]: .*path"),
        (MINIMAL + "[proxmox]\nurl = 'https://pve'\n", r"\[proxmox\]: .*token_id"),
    ],
)
def test_invalid_content(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, text))


def test_verify_tls_is_refused_with_a_hint(tmp_path):
    text = MINIMAL + "[proxmox]\nurl = 'https://pve'\ntoken_id = 'a@pve!b'\nverify_tls = false\n"
    with pytest.raises(ConfigError, match="ca_file"):
        load_config(_write(tmp_path, text))


def test_secret_from_value_or_file(tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file\n")
    text = MINIMAL + f"[proxmox]\nurl = 'https://pve'\ntoken_id = 'a@pve!b'\ntoken_secret_file = '{secret_file.as_posix()}'\n"
    assert load_config(_write(tmp_path, text)).proxmox.secret() == "from-file"

    text = MINIMAL + "[proxmox]\nurl = 'https://pve'\ntoken_id = 'a@pve!b'\ntoken_secret = 'inline'\n"
    assert load_config(_write(tmp_path, text)).proxmox.secret() == "inline"


def test_missing_secret(tmp_path):
    text = MINIMAL + "[proxmox]\nurl = 'https://pve'\ntoken_id = 'a@pve!b'\n"
    with pytest.raises(ConfigError, match="token_secret"):
        load_config(_write(tmp_path, text)).proxmox.secret()


def test_target_selection():
    cfg = load_config(EXAMPLE)
    assert cfg.target("nas").path == "/mnt/nas/antigrafa"
    with pytest.raises(ConfigError, match="several targets.*local, nas"):
        cfg.target(None)
    with pytest.raises(ConfigError, match="unknown target: usb"):
        cfg.target("usb")

    single = Config(None, SSHConfig(), {}, {"local": cfg.targets["local"]}, Tuning())
    assert single.target(None) is cfg.targets["local"]
