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
    assert pve.ssh.options == ["StrictHostKeyChecking=accept-new"]  # first connection without a prompt
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


def test_snapshot_mode_setting(tmp_path):
    assert load_config(_write(tmp_path, MINIMAL + PROXMOX)).source("pve").settings.snapshot_mode == "proxmox"
    text = MINIMAL + PROXMOX + 'snapshot_mode = "storage"\n'
    assert load_config(_write(tmp_path, text)).source("pve").settings.snapshot_mode == "storage"
    for bad in ('"zfs"', "true"):
        with pytest.raises(ConfigError, match=r"\[source.pve\]: snapshot_mode must be one of proxmox, storage"):
            load_config(_write(tmp_path, MINIMAL + PROXMOX + f"snapshot_mode = {bad}\n"))


def test_snapshot_mode_per_guest(tmp_path):
    text = MINIMAL + PROXMOX + (
        'snapshot_mode = "auto"\n'
        '[source.pve.guests.650]\nsnapshot_mode = "qemu"\n'
        "[source.pve.guests.911]\n"
    )
    settings = load_config(_write(tmp_path, text)).source("pve").settings
    assert [settings.mode_for(g) for g in ("650", "911", "100")] == ["qemu", "auto", "auto"]
    bad = {
        '[source.pve.guests.650]\nsnapshot_mode = "zfs"\n': r"\[source.pve.guests.650\]: snapshot_mode must be one of",
        '[source.pve.guests.web]\nsnapshot_mode = "qemu"\n': r"\[source.pve.guests.web\]: a guest is named by its id",
        "[source.pve.guests.650]\nchunk = 4\n": r"\[source.pve.guests.650\]: unknown keys chunk",
    }
    for section, error in bad.items():
        with pytest.raises(ConfigError, match=error):
            load_config(_write(tmp_path, MINIMAL + PROXMOX + section))


# --------------------------------------------------------------------------- web interface


def test_web_section_defaults_and_values(tmp_path):
    from antigrafa.config import WebConfig, default_web_dir

    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.web == WebConfig()
    assert cfg.web.port == 8443 and cfg.web.listen == "0.0.0.0"
    assert cfg.web.directory == Path(default_web_dir())
    assert cfg.web.certificate == Path(default_web_dir()) / "tls" / "cert.pem"
    assert cfg.web.private_key == Path(default_web_dir()) / "tls" / "key.pem"

    cfg = load_config(_write(tmp_path, MINIMAL + """
[web]
listen = "10.0.0.5"
port = 443
data_dir = "/srv/antigrafa-web"
cert_file = "/etc/ssl/backup.pem"
key_file = "/etc/ssl/backup.key"
allowed_hosts = ["backup.lab"]
"""))
    assert cfg.web.listen == "10.0.0.5" and cfg.web.port == 443
    assert cfg.web.directory == Path("/srv/antigrafa-web")
    assert cfg.web.certificate == Path("/etc/ssl/backup.pem")
    assert cfg.web.private_key == Path("/etc/ssl/backup.key")
    assert cfg.web.allowed_hosts == ["backup.lab"]


@pytest.mark.parametrize(
    ("web", "message"),
    [
        ("port = 0", "port must be between 1 and 65535"),
        ("port = 70000", "port must be between 1 and 65535"),
        ("port = true", "port must be between 1 and 65535"),
        ("port = \"8443\"", "port must be between 1 and 65535"),
        ("listen = \"\"", "listen must not be empty"),
        ("listen = 5", "listen must be a string"),
        ("allowed_hosts = \"backup.lab\"", "allowed_hosts must be a list"),
        ("allowed_hosts = [\"\"]", "allowed_hosts must be a list"),
        ("cert_file = \"/etc/ssl/a.pem\"", "cert_file and key_file go together"),
        ("colour = \"blue\"", "unknown keys colour"),
    ],
)
def test_invalid_web_section(tmp_path, web, message):
    with pytest.raises(ConfigError, match=rf"\[web\]: .*{message}"):
        load_config(_write(tmp_path, MINIMAL + "[web]\n" + web + "\n"))


def test_default_paths_on_linux_and_windows():
    from antigrafa.config import default_config_path, default_web_dir

    assert default_config_path("linux", {}) == "/etc/antigrafa/antigrafa.toml"
    assert default_web_dir("linux", {}) == "/var/lib/antigrafa/web"
    env = {"PROGRAMDATA": r"D:\ProgramData"}
    assert default_config_path("win32", env) == r"D:\ProgramData\antigrafa\antigrafa.toml"
    assert default_web_dir("win32", env) == r"D:\ProgramData\antigrafa\web"
    assert default_web_dir("win32", {}) == r"C:\ProgramData\antigrafa\web"  # variable missing


def test_default_log_file_on_linux_and_windows():
    from antigrafa.config import default_log_file

    assert default_log_file("linux", {}) == "/var/log/antigrafa/antigrafa.log"
    env = {"PROGRAMDATA": r"D:\ProgramData"}
    assert default_log_file("win32", env) == r"D:\ProgramData\antigrafa\antigrafa.log"


# --------------------------------------------------------------------------- log file


def test_log_section_defaults_and_values(tmp_path):
    from antigrafa import config
    from antigrafa.config import LogConfig

    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.log == LogConfig()
    assert cfg.log.enabled and cfg.log.path == Path(config.default_log_file())
    assert cfg.log.max_bytes == 1024**3  # 1 GiB
    assert (cfg.log.max_archives, cfg.log.compression, cfg.log.compression_level) == (5, "zip", 6)

    cfg = load_config(_write(tmp_path, MINIMAL + """
[log]
file = "/srv/logs/backup.log"
max_size_mib = 100
max_archives = 10
compression = "tar.gz"
compression_level = 9
"""))
    assert cfg.log.path == Path("/srv/logs/backup.log") and cfg.log.max_bytes == 100 * 1024**2
    assert (cfg.log.max_archives, cfg.log.compression, cfg.log.compression_level) == (10, "tar.gz", 9)
    assert not load_config(_write(tmp_path, MINIMAL + '[log]\nfile = "none"\n')).log.enabled


@pytest.mark.parametrize(
    ("log", "message"),
    [
        ("max_size_mib = 0", "max_size_mib must be an integer >= 1"),
        ("max_size_mib = 1.5", "max_size_mib must be an integer >= 1"),
        ("max_archives = -1", "max_archives must be an integer >= 0"),
        ("max_archives = true", "max_archives must be an integer >= 0"),
        ("compression = \"arj\"", "compression must be one of zip, tar.gz, rar, 7z"),
        ("compression = \"7z\"\ncompression_level = 10", "compression_level must be between 0 and 9 for 7z"),
        ("compression = \"rar\"\ncompression_level = 6", "compression_level must be between 0 and 5 for rar"),
        ("compression_level = 10", "compression_level must be between 0 and 9 for zip"),
        ("compression_level = -1", "compression_level must be between 0 and 9"),
        ("file = 5", "file must be a string"),
        ("size = 5", "unknown keys size"),
    ],
)
def test_invalid_log_section(tmp_path, log, message):
    with pytest.raises(ConfigError, match=rf"\[log\]: .*{message}"):
        load_config(_write(tmp_path, MINIMAL + "[log]\n" + log + "\n"))


def test_log_compression_level_default_depends_on_the_compression(tmp_path):
    rar = load_config(_write(tmp_path, MINIMAL + '[log]\ncompression = "rar"\n')).log
    assert (rar.compression, rar.compression_level) == ("rar", 3)  # rar -m3, its own default
    gz = load_config(_write(tmp_path, MINIMAL + '[log]\ncompression = "tar.gz"\n')).log
    assert gz.compression_level == 6
    best = load_config(_write(tmp_path, MINIMAL + '[log]\ncompression = "rar"\ncompression_level = 5\n')).log
    assert best.compression_level == 5


def test_log_7z_default_level(tmp_path):
    seven = load_config(_write(tmp_path, MINIMAL + '[log]\ncompression = "7z"\n')).log
    assert (seven.compression, seven.compression_level) == ("7z", 5)  # 7z -mx5, its own default
    best = load_config(_write(tmp_path, MINIMAL + '[log]\ncompression = "7z"\ncompression_level = 9\n')).log
    assert best.compression_level == 9


JOB = MINIMAL + """
[job.nightly]
all = true
exclude = [9000, "105"]
schedule = "0 2 * * *"
"""


def test_jobs_and_scheduler_sections(tmp_path):
    from antigrafa.config import default_scheduler_dir

    cfg = load_config(_write(tmp_path, JOB + PROXMOX + '\n[scheduler]\ndata_dir = "/srv/sched"\n'))
    job = cfg.jobs["nightly"]
    assert job.all and job.exclude == ["9000", "105"] and job.enabled  # ids as text
    assert job.timing.describe() == "cron 0 2 * * *" and job.source == "" and job.target == ""
    assert cfg.scheduler.directory == Path("/srv/sched")
    assert load_config(_write(tmp_path, MINIMAL)).jobs == {}
    assert str(load_config(_write(tmp_path, MINIMAL)).scheduler.directory) == default_scheduler_dir()


def test_scheduler_default_directory():
    from antigrafa.config import default_scheduler_dir

    assert default_scheduler_dir(platform="linux") == "/var/lib/antigrafa/scheduler"
    assert default_scheduler_dir(platform="win32", environ={"PROGRAMDATA": r"D:\Data"}) == \
        r"D:\Data\antigrafa\scheduler"


def test_job_forms(tmp_path):
    text = MINIMAL + PROXMOX + """
[job.a]
guests = [100, "101"]
every = "6h"
target = "local"
source = "pve"

[job.b]
all = true
at = ["02:00", "14:00"]
days = ["mon", "fri"]
enabled = false
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.jobs["a"].guests == ["100", "101"] and cfg.jobs["a"].timing.describe() == "every 6h"
    assert cfg.jobs["b"].timing.describe() == "at 02:00, 14:00 on mon, fri" and not cfg.jobs["b"].enabled


@pytest.mark.parametrize(
    ("job", "message"),
    [
        ("all = true", r"\[job.j\]: set one of schedule"),
        ('schedule = "@daily"', "set all = true, or the guests"),
        ('all = true\nguests = ["1"]\nschedule = "@daily"', "set all = true, or the guests"),
        ('all = "yes"\nschedule = "@daily"', "all must be true or false"),
        ('all = true\nschedule = "@daily"\nenabled = 1', "enabled must be true or false"),
        ('guests = "100"\nschedule = "@daily"', "guests must be a list of guest ids"),
        ('all = true\nexclude = [true]\nschedule = "@daily"', "exclude must be a list"),
        ('all = true\nschedule = "0 2 * *"', "needs 5 fields"),
        ('all = true\nevery = "1x"', "m, h or d"),
        ('all = true\nschedule = "@daily"\nsource = "other"', "unknown source 'other' \(pve\)"),
        ('all = true\nschedule = "@daily"\ntarget = "nas"', "unknown target 'nas' \(local\)"),
        ('all = true\nschedule = "@daily"\nsource = 3', "source must be a string"),
        ('all = true\nschedule = "@daily"\nwhen = "now"', "unknown keys when"),
    ],
)
def test_invalid_job(tmp_path, job, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, MINIMAL + PROXMOX + "\n[job.j]\n" + job + "\n"))


def test_job_needs_a_source_and_picks_the_only_one(tmp_path):
    with pytest.raises(ConfigError, match=r"\[job.j\]: a \[source.<name>\] section is required"):
        load_config(_write(tmp_path, MINIMAL + '[job.j]\nall = true\nevery = "1d"\n'))
    two = MINIMAL + PROXMOX + PROXMOX.replace("source.pve", "source.pve2")
    with pytest.raises(ConfigError, match="several sources configured, set source \(pve, pve2\)"):
        load_config(_write(tmp_path, two + '[job.j]\nall = true\nevery = "1d"\n'))
    with pytest.raises(ConfigError, match="invalid job name"):
        load_config(_write(tmp_path, MINIMAL + PROXMOX + '[job."a b"]\nall = true\nevery = "1d"\n'))
    with pytest.raises(ConfigError, match=r"\[scheduler\]: unknown keys x"):
        load_config(_write(tmp_path, MINIMAL + "[scheduler]\nx = 1\n"))
    with pytest.raises(ConfigError, match="data_dir must be a string"):
        load_config(_write(tmp_path, MINIMAL + "[scheduler]\ndata_dir = 1\n"))
