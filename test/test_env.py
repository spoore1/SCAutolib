# author: Pavel Yadlouski <pyadlous@redhat.com>
# Unit tests for of SCAutolib.src.env module
import re
from os import stat, mkdir
from os.path import isfile

from SCAutolib.src.env import *
from SCAutolib.src.exceptions import *
from SCAutolib.test.fixtures import *
from dotenv import load_dotenv
from pytest import raises
from yaml import load, FullLoader


def test_create_sssd_config(tmpdir, loaded_env, clean_conf):
    """Check correct creation og sssd.conf with basic sections and permission."""
    # Arrange
    sssd_conf = "/etc/sssd/sssd.conf"
    if exists(sssd_conf):
        remove(sssd_conf)

    # Act
    create_sssd_config()
    cnf = ConfigParser()
    cnf.optionxform = str
    with open(sssd_conf, "r") as file:
        cnf.read_file(file)
    sections = cnf.sections()
    perms = oct(stat(sssd_conf).st_mode & 0o777)

    assert "sssd" in sections
    assert "pam" in sections
    assert "nss" in sections
    assert "domain/shadowutils" in sections
    assert perms == oct(0o600), "wrong permission on sssd.conf"


def test_create_cnf(tmpdir):
    username = "test-user"
    conf_dir = f"{tmpdir}/test-user"
    mkdir(conf_dir)

    create_cnf(username, conf_dir)
    assert isfile(join(conf_dir, f"req_{username}.cnf"))


def test_create_cnf_ca(prep_ca):
    username = "ca"
    ca_dir = prep_ca
    conf_dir = f"{ca_dir}/conf"
    ca_cnf = join(conf_dir, "ca.cnf")

    create_cnf(username, conf_dir)
    assert isfile(ca_cnf)

    with open(ca_cnf, "r") as f:
        content = f.read()
    assert re.findall(f"dir[ ]*=[ ]*{ca_dir}", content)


def test_create_cnf_exception():
    username = "test-user"

    with raises(UnspecifiedParameter):
        create_cnf(username)


def test_create_softhsm2_config():
    card_dir = "/tmp/card-dir"
    conf_dir = join(card_dir, "conf")
    softhsm2_config = join(conf_dir, "softhsm2.conf")
    mkdir(card_dir)
    mkdir(conf_dir)
    create_softhsm2_config(card_dir)
    try:
        assert isfile(softhsm2_config)
        with open(softhsm2_config, "r") as f:
            content = f.read()
        assert re.findall(f"directories.tokendir[ ]*=[ ]*{card_dir}/tokens/",
                          content)
    finally:
        rmtree(card_dir)


def test_create_virt_card_service():
    username = "test-user"
    service_path = f"/etc/systemd/system/virt_cacard_{username}.service"
    card_dir = "/tmp/card-dir"
    conf_dir = f"{card_dir}/conf"
    mkdir(card_dir)
    mkdir(conf_dir)
    cnf = ConfigParser()
    create_virt_card_service(username, card_dir)

    with open(service_path, "r") as f:
        cnf.read_file(f)
    try:
        assert isfile(service_path)
        assert f"SOFTHSM2_CONF=\"{conf_dir}/softhsm2.conf\"" == cnf.get(
            "Service", "Environment")
        assert f"{card_dir}" == cnf.get("Service", "WorkingDirectory")
        assert f"virtual card for {username}" == cnf.get("Unit", "Description")
    finally:
        rmtree(card_dir)
        remove(service_path)


def test_check_config_true(config_file_correct, caplog):
    result = check_config(config_file_correct)
    assert result
    assert "Configuration file is OK." in caplog.messages


def test_check_config_false(config_file_incorrect, caplog):
    result = check_config(config_file_incorrect)
    assert not result
    assert "Configuration file is OK." not in caplog.messages
    assert "Field root_passwd is not present in the config." in caplog.messages


def test_load_env(config_file_correct):
    env_path = load_env(config_file_correct)
    assert exists(env_path)
    load_dotenv(env_path)
    for field in ("TMP", "KEYS", "CERTS", "BACKUP", "CONF", "CA_DIR"):
        assert field in environ
    with open(config_file_correct, "r") as f:
        data = load(f, Loader=FullLoader)
    assert "restore" in data.keys()
    assert len(data["restore"]) == 0


def test_add_restore(loaded_env, clean_conf):
    env_path, _ = loaded_env
    src = '/src/some.file'
    dest = '/dest/some.file'

    add_restore("file", src, dest)
    load_dotenv(env_path)

    with open(environ["CONF"], "r") as f:
        data = load(f, Loader=FullLoader)

    assert len(data["restore"]) == 1

    restore = data["restore"][0]

    assert restore["type"] == "file"
    assert restore["backup_dir"] == dest
    assert restore["src"] == src


def test_add_restore_wrong_type(caplog, loaded_env, clean_conf):
    env_path, _ = loaded_env

    add_restore("file", "src", "dest")
    add_restore("wrong_type", "src", "dest")

    load_dotenv(env_path)
    with open(environ["CONF"], "r") as f:
        data = load(f, Loader=FullLoader)

    assert len(data["restore"]) == 2

    restore = data["restore"][0]
    msg = "Type wrong_type is not know, so this item can't be correctly restored"
    assert restore["type"] == "file"
    assert restore["backup_dir"] == "dest"
    assert restore["src"] == "src"
    assert msg in caplog.messages


def test_setup_ca(prep_ca, caplog):
    """Test for secess setup of local CA."""
    ca_dir = prep_ca
    create_cnf("ca")
    setup_ca_()

    # Assert
    assert "Setup of local CA is completed" in caplog.messages
    assert exists(f"{ca_dir}/rootCA.pem")
    assert exists(f"{ca_dir}/rootCA.key")

    with open(f"{ca_dir}/rootCA.pem", "r") as f:
        root_crt = f.read()

    with open("/etc/sssd/pki/sssd_auth_ca_db.pem", "r") as f:
        ca_db = f.read()

    assert root_crt in ca_db


@pytest.mark.service_restart()
def test_create_sc(prep_ca_real, caplog):
    user = read_config("local_user")
    card_dir = user["card_dir"]
    cert, key = join(card_dir, f"{user['name']}.crt"), join(
        card_dir, f"{user['name']}.key")

    create_sc(user)

    assert exists(f"/etc/systemd/system/virt_cacard_{user['name']}.service"), \
        "service for the virtual smart card not exists"

    matchrule = f"""[certmap/shadowutils/{user['name']}]
matchrule = <SUBJECT>.*CN={user['name']}.*"""
    with open("/etc/sssd/sssd.conf", "r") as f:
        content = f.read()
    assert matchrule in content, "matchrule is not present in the sssd.conf"

    assert exists("/etc/systemd/system/pcscd.service"), "pcscd.service is not " \
                                                        "copied"
    with open("/etc/systemd/system/pcscd.service", "r") as f:
        data = f.read()
    assert "--auto-exit" not in data

    assert exists(key), "User private key isn't created"
    assert exists(cert), "User certificate isn't created"