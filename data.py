from dataclasses import field, dataclass
from enum import Enum
from typing import List

VERSION = "1.4"

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

    def __eq__(self, other):
        return self.x == other.x and self.y == other.y

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

    def invertY(self):
        return Offset(self.x, -self.y)


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
            self.reset()

    def reset(self):
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
    directions: List[AnimationSequence] = field(default_factory=lambda: [AnimationSequence() for _ in range(8)])  # 8 directions.
    copyName: str = field(compare=False, default='')  # If it is a copy of another group.
    modified: bool = field(compare=False, default=False)  # If it has been modified since loading.


@dataclass
class Rectangle:
    x: int
    y: int
    width: int
    height: int

    @property
    def left(self):
        return self.x

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y

    @property
    def top(self):
        return self.y + self.height

    def values(self):
        return self.x, self.y, self.width, self.height

    def __add__(self, other):
        if isinstance(other, tuple):
            return Rectangle(self.x + other[0], self.y + -other[1], self.width, self.height)
        else:
            return Rectangle(self.x + other.x, self.y + other.y, self.width, self.height)

    def __repr__(self):
        return f"Rectangle(x={self.x}, y={self.y}, width={self.width}, height={self.height}, ltrb={(self.left, self.top, self.right, self.bottom)})"


@dataclass
class TLRectangle:
    x: int
    y: int
    width: int
    height: int

    @property
    def left(self):
        return self.x

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def top(self):
        return self.y

    def values(self):
        return self.x, self.y, self.width, self.height

    def __add__(self, other):
        if isinstance(other, tuple):
            return TLRectangle(self.x + other[0], self.y + other[1], self.width, self.height)
        else:
            return TLRectangle(self.x + other.x, self.y + other.y, self.width, self.height)

    @classmethod
    def fromBounds(cls, bounds: tuple[int, int, int, int]):
        return cls(bounds[0], bounds[1], bounds[2]-bounds[0], bounds[3]-bounds[1])

    def getFlip(self):
        return TLRectangle(-self.right, self.y, self.width, self.height)

    def __repr__(self):
        return f"TLRectangle(x={self.x}, y={self.y}, width={self.width}, height={self.height}, ltrb={(self.left, self.top, self.right, self.bottom)})"

def centerBounds(rect: Rectangle):
    minX = min(rect.x, -rect.right)
    minY = min(rect.y, -rect.top)

    maxX = max(-rect.x, rect.right)
    maxY = max(-rect.y, rect.top)
    return Rectangle(minX, minY, maxX - minX, maxY - minY)

@dataclass
class ActionPoints:
    leftHand: Offset = Offset() # red[1]
    center: Offset = Offset()  # green[2]
    rightHand: Offset = Offset()  # blue[3]
    head: Offset = Offset() # black
    shadow: Offset = Offset()  # white[4]

    @property
    def centerFlip(self):
        return self._flip(self.center)

    @property
    def headFlip(self):
        return self._flip(self.head)

    @property
    def leftHandFlip(self):
        return self._flip(self.leftHand)

    @property
    def rightHandFlip(self):
        return self._flip(self.rightHand)

    def allPos(self):
        return self.leftHand, self.center, self.rightHand, self.head

    def allFlipPos(self):
        return self.leftHandFlip, self.centerFlip, self.rightHandFlip, self.headFlip

    def add(self, offset: Offset):
        self.leftHand += offset
        self.center += offset
        self.rightHand += offset
        self.head += offset

    def flip(self, offset: Offset, width: int):
        return Offset(width-offset.x - 1, offset.y)

    def _flip(self, offset: Offset):
        return Offset(-offset.x - 1, offset.y)

    def equals(self, other: 'ActionPoints', flip: bool, oddWidth: bool):
        # print("equals", self)
        # print("other", other)
        center = other.center
        head = other.head
        leftHand = other.leftHand
        rightHand = other.rightHand

        if flip:
            #print("--- flip", width)
            # center = self.flip(other.center, width) + Offset(1 if oddWidth else 0, 0)
            # head = self.flip(other.head, width) + Offset(1 if oddWidth else 0, 0)
            # leftHand = self.flip(other.leftHand, width) + Offset(1 if oddWidth else 0, 0)
            # rightHand = self.flip(other.rightHand, width) + Offset(1 if oddWidth else 0, 0)

            center = other.centerFlip + Offset(1 if oddWidth else 0, 0)
            head = other.headFlip + Offset(1 if oddWidth else 0, 0)
            leftHand = other.leftHandFlip + Offset(1 if oddWidth else 0, 0)
            rightHand = other.rightHandFlip + Offset(1 if oddWidth else 0, 0)
            #
            #
            # print(leftHand, self.leftHand)
            # print(center, self.center)
            # print(rightHand, self.rightHand)
            # print(head, self.head)

        if self.center != center:
            return False
        if self.head != head:
            return False
        if self.leftHand != leftHand:
            return False
        if self.rightHand != rightHand:
            return False

        return True

    # Top left
    # def getRect(self):
    #     top = min(min(self.center.y, self.head.y), min(self.leftHand.y, self.rightHand.y))
    #     left = min(min(self.center.x, self.head.x), min(self.leftHand.x, self.rightHand.x))
    #     bottom = max(max(self.center.y, self.head.y), max(self.leftHand.y, self.rightHand.y)) + 1
    #     right = max(max(self.center.x, self.head.x), max(self.leftHand.x, self.rightHand.x)) + 1
    #     return Rectangle(left, top, right - left, bottom - top)

    def getRect(self):
        left = min(min(self.center.x, self.head.x), min(self.leftHand.x, self.rightHand.x))
        top = min(min(self.center.y, self.head.y), min(self.leftHand.y, self.rightHand.y))
        right = max(max(self.center.x, self.head.x), max(self.leftHand.x, self.rightHand.x)) + 1
        bottom = max(max(self.center.y, self.head.y), max(self.leftHand.y, self.rightHand.y)) + 1

        return Rectangle(left, bottom, right-left, top-bottom)