{
  lib,
  stdenv,
  python3Packages,
  fetchFromGitHub,
  makeWrapper,
  playwright-driver,
}:

python3Packages.buildPythonApplication rec {
  pname = "lucidadl";
  version = "1.4.0";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "Jude-A";
    repo = "lucidadl";
    rev = "v${version}";
    hash = "sha256-KpnhhgBh8YYrfcZFklX72NQR+4mRKe674hIi2btUyS0=";
  };

  nativeBuildInputs = [
    python3Packages.setuptools
    makeWrapper
  ];

  propagatedBuildInputs = with python3Packages; [
    playwright
    click
    httpx
    h2
    pyjson5
    imageio-ffmpeg
    mutagen
    rich
    questionary
  ];

  # Nix provides Playwright browsers in a separate immutable store path.
  # Point the Python driver at that bundle so setup never tries to download
  # Chromium into the user's environment.
  postFixup = ''
    wrapProgram $out/bin/lucida \
      --set PLAYWRIGHT_BROWSERS_PATH "${playwright-driver.browsers-chromium}"
    wrapProgram $out/bin/lucidadl \
      --set PLAYWRIGHT_BROWSERS_PATH "${playwright-driver.browsers-chromium}"
  '';

  meta = with lib; {
    description = "Lucida downloader CLI for tracks, albums and public playlists";
    homepage = "https://github.com/Jude-A/lucidadl";
    license = licenses.mit;
    mainProgram = "lucidadl";
    platforms = platforms.linux;
  };
}
