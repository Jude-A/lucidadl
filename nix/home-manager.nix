{ config, lib, pkgs, ... }:

let
  cfg = config.programs.lucidadl;
in
{
  options.programs.lucidadl = {
    enable = lib.mkEnableOption "lucidadl";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./default.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./default.nix { }";
      description = "The lucidadl package to install.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];
  };
}