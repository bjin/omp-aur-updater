# oh-my-pi AUR updater and pacman repository

This repository updates both the [`oh-my-pi`](https://aur.archlinux.org/packages/oh-my-pi) and [`oh-my-pi-bin`](https://aur.archlinux.org/packages/oh-my-pi-bin) AUR packages. Newly built `oh-my-pi` packages are also published as a signed pacman repository in the fixed GitHub Release [`oh-my-pi`](https://github.com/bjin/omp-aur-updater/releases/tag/oh-my-pi).

The release contains:

- built package files, preserving the filenames that contain `pkgver` and `pkgrel`;
- one detached `.sig` file per package;
- `oh-my-pi.db` and its detached `oh-my-pi.db.sig` signature.

At least the five newest packages are retained. An older package and its signature are deleted only when the package is at least seven days old **and** at least five newer package files exist.

## Trust the repository signing key

Download the public key tracked in this repository:

```sh
curl -fsSLo /tmp/oh-my-pi-repo.asc \
  https://raw.githubusercontent.com/bjin/omp-aur-updater/main/keys/oh-my-pi-repo.asc
```

Verify its fingerprint before trusting it:

```sh
gpg --show-keys --with-fingerprint /tmp/oh-my-pi-repo.asc
```

The expected fingerprint is:

```text
F357 77CF E7F0 794D 9233 8EA5 2FAB E6F5 2B58 6BAB
```

Initialize the pacman keyring first if the system has not already done so:

```sh
sudo pacman-key --init
sudo pacman-key --populate archlinux
```

Import and locally trust the repository key:

```sh
sudo pacman-key --add /tmp/oh-my-pi-repo.asc
sudo pacman-key --lsign-key F35777CFE7F0794D92338EA52FABE6F52B586BAB
rm /tmp/oh-my-pi-repo.asc
```

## Add the repository to pacman

Append this section to `/etc/pacman.conf`:

```ini
[oh-my-pi]
SigLevel = Required
Server = https://github.com/bjin/omp-aur-updater/releases/download/oh-my-pi
```

`SigLevel = Required` makes pacman verify both the repository database and every downloaded package. Pacman follows GitHub's release-asset redirect and obtains package filenames directly from `oh-my-pi.db`.

Refresh package databases and install the package:

```sh
sudo pacman -Syu oh-my-pi
```
