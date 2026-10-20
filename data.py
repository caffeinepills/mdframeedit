from __future__ import annotations

from dataclasses import field, dataclass
from enum import Enum

VERSION = "2.0"

RIGHT = 6
UP_RIGHT = 5
UP = 4
UP_LEFT = 3
LEFT = 2
DOWN_LEFT = 1
DOWN = 0
DOWN_RIGHT = 7

FRAME_DATA = (DOWN, DOWN_LEFT, LEFT, UP_LEFT, UP, UP_RIGHT, RIGHT, DOWN_RIGHT)

FD_STR = {
    DOWN: "DOWN",
    DOWN_LEFT: "DOWN LEFT",
    LEFT: "LEFT",
    UP_LEFT: "UP LEFT",
    UP: "UP",
    UP_RIGHT: "UP RIGHT",
    RIGHT: "RIGHT",
    DOWN_RIGHT: "DOWN RIGHT"
}


class SheetFormat(Enum):
    SINGLE = "FrameData.xml"
    MULTI = "AnimData.xml"


@dataclass(slots=True)
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

    def invert_y(self):
        return Offset(self.x, -self.y)

    def data(self):
        return self.x, self.y


@dataclass(slots=True)
class AnimFrame:
    idx: int = 0  # (Not used in frame, used for indexing slider points)
    frame_index: int = 0  # MetaFrameGroupIndex (actual frame in sheet.)
    flip: int = 0  # HFlip
    duration: int = 0  # Duration (total accumulation)
    shadow_offset: Offset = field(default_factory=Offset)  # Shadow
    sprite_offset: Offset = field(default_factory=Offset)  # Sprite
    is_default_copy: bool = field(compare=False, default=False)
    default_copy: AnimFrame | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        if not self.is_default_copy:
            self.reset()

    def reset(self):
        self.default_copy = AnimFrame(self.idx, self.frame_index, self.flip, self.duration,
                                     Offset(self.shadow_offset.x, self.shadow_offset.y),
                                     Offset(self.sprite_offset.x, self.sprite_offset.y),
                                     is_default_copy=True)

    @property
    def changed(self):
        return self.default_copy is not None and self != self.default_copy


@dataclass(slots=True)
class AnimationSequence:
    frames: list[AnimFrame] = field(default_factory=list)


@dataclass(slots=True)
class AnimGroup:
    idx: int
    name: str
    rush_frame: int = -1
    hit_frame: int = -1
    return_frame: int = -1
    directions: list[AnimationSequence] = field(default_factory=lambda: [AnimationSequence() for _ in range(8)])  # 8 directions.
    copy_name: str = field(compare=False, default='')  # If it is a copy of another group.
    modified: bool = field(compare=False, default=False)  # If it has been modified since loading.

    def __post_init__(self):
        if len(self.directions) == 1:
            self.set_single_direction(self.directions[0])

    @property
    def is_single_direction(self):
        """One shared sequence, rather than eight independently editable ones."""
        return len(self.directions) == 8 and all(sequence is self.directions[0] for sequence in self.directions)

    def set_single_direction(self, sequence):
        # Keep compass indexing available without turning a shared sequence
        # into eight separate copies that diverge as soon as one is edited.
        self.directions = [sequence] * 8

    def replace_sequence(self, direction, sequence):
        if self.is_single_direction:
            self.set_single_direction(sequence)
        else:
            self.directions[direction] = sequence

    def event_frames(self, sequence):
        """Effective event positions; omitted XML values remain -1 for saving."""
        last = len(sequence.frames) - 1
        returned = self.return_frame if self.return_frame >= 0 else last
        hit = self.hit_frame if self.hit_frame >= 0 else returned
        rush = self.rush_frame if self.rush_frame >= 0 else (0 if last >= 0 else -1)
        return rush, hit, returned


@dataclass(slots=True)
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


@dataclass(slots=True)
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

    def bounds(self):
        return self.left, self.top, self.right, self.bottom

    @property
    def center(self):
        return self.x + self.width // 2, self.y + self.height // 2

    def __add__(self, other):
        if isinstance(other, tuple):
            return TLRectangle(self.x + other[0], self.y + other[1], self.width, self.height)
        else:
            return TLRectangle(self.x + other.x, self.y + other.y, self.width, self.height)

    @classmethod
    def from_bounds(cls, bounds: tuple[int, int, int, int]):
        return cls(bounds[0], bounds[1], bounds[2]-bounds[0], bounds[3]-bounds[1])

    def get_flip(self):
        return TLRectangle(-self.right, self.y, self.width, self.height)

    def __repr__(self):
        return f"TLRectangle(x={self.x}, y={self.y}, width={self.width}, height={self.height}, ltrb={(self.left, self.top, self.right, self.bottom)})"

def center_bounds(rect: Rectangle):
    min_x = min(rect.x, -rect.right)
    min_y = min(rect.y, -rect.top)

    max_x = max(-rect.x, rect.right)
    max_y = max(-rect.y, rect.top)
    return Rectangle(min_x, min_y, max_x - min_x, max_y - min_y)

@dataclass(slots=True)
class ActionPoints:
    left_hand: Offset = field(default_factory=Offset) # red[1]
    center: Offset = field(default_factory=Offset)  # green[2]
    right_hand: Offset  = field(default_factory=Offset) # blue[3]
    head: Offset  = field(default_factory=Offset) # black
    shadow: Offset = field(default_factory=Offset)  # white[4]

    @property
    def center_flip(self):
        return self._flip(self.center)

    @property
    def head_flip(self):
        return self._flip(self.head)

    @property
    def left_hand_flip(self):
        return self._flip(self.left_hand)

    @property
    def right_hand_flip(self):
        return self._flip(self.right_hand)

    def empty(self):
        if (self.left_hand.x == 0 and self.left_hand.y == 0 and self.right_hand.x == 0 and self.right_hand.y == 0 and
                self.head.x == 0 and self.head.y == 0 and self.center.x == 0 and self.center.y == 0):
            return True

        return False

    def all_pos(self):
        return self.left_hand, self.center, self.right_hand, self.head

    def all_flip_pos(self):
        return self.left_hand_flip, self.center_flip, self.right_hand_flip, self.head_flip

    def add(self, offset: Offset):
        self.left_hand += offset
        self.center += offset
        self.right_hand += offset
        self.head += offset

    def flip(self, offset: Offset, width: int):
        return Offset(width-offset.x - 1, offset.y)

    def _flip(self, offset: Offset):
        return Offset(-offset.x - 1, offset.y)

    def equals(self, other: 'ActionPoints', flip: bool, odd_width: bool):
        # print("equals", self)
        # print("other", other)
        center = other.center
        head = other.head
        left_hand = other.left_hand
        right_hand = other.right_hand

        if flip:
            #print("--- flip", width)
            # center = self.flip(other.center, width) + Offset(1 if odd_width else 0, 0)
            # head = self.flip(other.head, width) + Offset(1 if odd_width else 0, 0)
            # left_hand = self.flip(other.left_hand, width) + Offset(1 if odd_width else 0, 0)
            # right_hand = self.flip(other.right_hand, width) + Offset(1 if odd_width else 0, 0)

            center = other.center_flip + Offset(1 if odd_width else 0, 0)
            head = other.head_flip + Offset(1 if odd_width else 0, 0)
            left_hand = other.left_hand_flip + Offset(1 if odd_width else 0, 0)
            right_hand = other.right_hand_flip + Offset(1 if odd_width else 0, 0)
            #
            #
            # print(left_hand, self.left_hand)
            # print(center, self.center)
            # print(right_hand, self.right_hand)
            # print(head, self.head)

        if self.center != center:
            return False
        if self.head != head:
            return False
        if self.left_hand != left_hand:
            return False
        if self.right_hand != right_hand:
            return False

        return True

    # Top left
    # def get_rect(self):
    #     top = min(min(self.center.y, self.head.y), min(self.left_hand.y, self.right_hand.y))
    #     left = min(min(self.center.x, self.head.x), min(self.left_hand.x, self.right_hand.x))
    #     bottom = max(max(self.center.y, self.head.y), max(self.left_hand.y, self.right_hand.y)) + 1
    #     right = max(max(self.center.x, self.head.x), max(self.left_hand.x, self.right_hand.x)) + 1
    #     return Rectangle(left, top, right - left, bottom - top)

    def get_rect(self):
        left = min(min(self.center.x, self.head.x), min(self.left_hand.x, self.right_hand.x))
        top = min(min(self.center.y, self.head.y), min(self.left_hand.y, self.right_hand.y))
        right = max(max(self.center.x, self.head.x), max(self.left_hand.x, self.right_hand.x)) + 1
        bottom = max(max(self.center.y, self.head.y), max(self.left_hand.y, self.right_hand.y)) + 1

        return Rectangle(left, bottom, right-left, top-bottom)


@dataclass(slots=True)
class EditState:
    """Data holder for edit state."""
    groups: list[AnimGroup]
    action_points: dict[int, ActionPoints]
    shadow_size: int
    shadow_size_present: bool
    action_row: int
    frame_row: int
    direction: int
    sheet_frame: int | None
    signature: tuple[object, ...]
