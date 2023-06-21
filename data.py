from dataclasses import field, dataclass
from enum import Enum
from typing import List

VERSION = "1.2"

RIGHT = 6
UP_RIGHT = 5
UP = 4
UP_LEFT = 3
LEFT = 2
DOWN_LEFT = 1
DOWN = 0
DOWN_RIGHT = 7

FRAME_DATA = (DOWN, DOWN_LEFT, LEFT, UP_LEFT, UP, UP_RIGHT, RIGHT, DOWN_RIGHT)


class LegacyAnimationActions(Enum):
    STOP = 0
    IDLE = 1
    WALK = 2
    SLEEP = 3
    HURT = 4
    ATK = 5
    CHARGE = 6
    SHOOT = 7
    STRIKE = 8
    CHOP = 9
    SCRATCH = 10
    PUNCH = 11
    SLAP = 12
    SLICE = 13
    MULTI_SCRATCH = 14
    MULTI_STRIKE = 15
    UPPERCUT = 16
    RICOCHET = 17
    BITE = 18
    SHAKE = 19
    JAB = 20
    KICK = 21
    LICK = 22
    SLAM = 23
    STOMP = 24
    APPEAL = 25
    DANCE = 26
    TWIRL = 27
    TAILWHIP = 28
    SING = 29
    SOUND = 30
    RUMBLE = 31
    FLAP_AROUND = 32
    GAS = 33
    SHOCK = 34
    EMIT = 35
    SPECIAL = 36
    WITHDRAW = 37
    REAR_UP = 38
    SWELL = 39
    SWING = 40
    DOUBLE = 41
    ROTATE = 42
    SPIN = 43
    JUMP = 44
    HIGH_JUMP = 45


@dataclass
class Offset:
    x: int = 0  # XOffset
    y: int = 0  # YOffset

    def __add__(self, other):
        return Offset(self.x + other.x,
                      self.y + other.y)

    def __sub__(self, other):
        return Offset(self.x - other.x,
                      self.y - other.y)

    def __mul__(self, other):
        if type(other) is int:
            return Offset(self.x * other,
                          self.y * other)
        else:
            return Offset(self.x * other.x,
                          self.y * other.y)

    def __truediv__(self, other):
        if type(other) is int:
            return Offset(self.x / other,
                          self.y / other)
        else:
            return Offset(self.x / other.x,
                          self.y / other.y)

    def __floordiv__(self, other):
        if type(other) is int:
            return Offset(self.x // other,
                          self.y // other)
        else:
            return Offset(self.x // other.x,
                          self.y // other.y)


@dataclass
class AnimFrame:
    idx: int = 0  # (Not used in frame, used for indexing slider points)
    frameIndex: int = 0  # MetaFrameGroupIndex (actual frame in sheet.)
    flip: int = 0  # HFlip
    duration: int = 0  # Duration (total accumulation)
    shadowOffset: Offset = field(default_factory=Offset)  # Shadow
    spriteOffset: Offset = field(default_factory=Offset)  # Sprite
    isDefaultCopy: bool = field(compare=False, default=False)

    def __post_init__(self):
        if not self.isDefaultCopy:
            self.defaultCopy = AnimFrame(self.idx, self.frameIndex, self.flip, self.duration,
                                         Offset(self.shadowOffset.x, self.shadowOffset.y),
                                         Offset(self.spriteOffset.x, self.spriteOffset.y),
                                         isDefaultCopy=True)

    @property
    def changed(self):
        return self != self.defaultCopy


@dataclass
class AnimationSequence:
    frames: List[AnimFrame] = field(default_factory=list)


@dataclass
class AnimGroup:
    idx: int
    name: str
    rushFrame: int = -1
    hitFrame: int = -1
    returnFrame: int = -1
    directions: List[AnimationSequence] = field(default_factory=lambda: [AnimationSequence()] * 8)  # 8 directions.
    copyName: str = field(compare=False, default='')  # If it is a copy of another group.
    modified: bool = field(compare=False, default=False)  # If it has been modified since loading.
