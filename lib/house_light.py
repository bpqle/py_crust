# Generated by the protocol buffer compiler.  DO NOT EDIT!
# sources: house_light.proto
# plugin: python-betterproto
from dataclasses import dataclass

import betterproto


@dataclass
class HlState(betterproto.Message):
    manual: bool = betterproto.bool_field(1)
    ephemera: bool = betterproto.bool_field(2)
    brightness: int = betterproto.int32_field(3)
    daytime: bool = betterproto.bool_field(4)


@dataclass
class HlParams(betterproto.Message):
    clock_interval: int = betterproto.int64_field(1)
