{
  # Flake metadata shown by `nix flake metadata` and similar commands.
  description = "Lucidadl nix-flake";

  # Pin nixpkgs so package/module behavior is reproducible.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      # Supported target systems for exported packages/apps.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      # Helper: build an attribute set for every supported system.
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      # Overlay exposing `pkgs.lucidadl` when this flake is imported elsewhere.
      overlays.default = final: prev: {
        lucidadl = final.callPackage ./nix { };
      };

      # Build package outputs for each supported system.
      packages = forAllSystems (
        system:
        let
          # Import nixpkgs for the current system.
          pkgs = import nixpkgs { inherit system; };
        in
        rec {
          # Package expression lives in `./nix/default.nix`.
          lucidadl = pkgs.callPackage ./nix { };
          # `nix build` defaults to this package.
          default = lucidadl;
        }
      );

      # NixOS module: install lucidadl system-wide and register the overlay.
      nixosModules.default =
        { pkgs, ... }:
        {
          nixpkgs.overlays = [ self.overlays.default ];
          environment.systemPackages = [ pkgs.lucidadl ];
        };

      # Home Manager module exported by this flake.
      homeModule = import ./nix/home-manager.nix;
      homeManagerModules.default = import ./nix/home-manager.nix;

      # `nix run` entrypoint -> the lucidadl executable from the default package.
      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/lucidadl";
        };
      });
    };
}