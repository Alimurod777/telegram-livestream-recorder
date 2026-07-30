from __future__ import annotations

import unittest

from livestream.metadata import SourceType, VideoSource
from livestream.priority_manager import PriorityManager
from livestream.source_manager import SourceManager
from livestream.state_machine import RecorderState, StateMachine


def source(
    source_id: int,
    source_type: SourceType,
    *,
    seen: float,
    width: int = 1280,
    height: int = 720,
) -> VideoSource:
    return VideoSource(
        source_id=source_id,
        source_type=source_type,
        width=width,
        height=height,
        last_seen=seen,
    )


class SourceSelectionTests(unittest.TestCase):
    def test_screen_share_preempts_camera_then_expires(self) -> None:
        manager = SourceManager()
        camera = source(20, SourceType.CAMERA, seen=10.0)
        screen = source(30, SourceType.SCREEN_SHARE, seen=11.0)
        manager.update_sources([camera, screen])

        selected = PriorityManager.get_active_source(manager.get_sources(), camera.source_id)
        self.assertEqual(selected, screen)

        refreshed_camera = source(20, SourceType.CAMERA, seen=13.0)
        manager.update_sources([refreshed_camera])
        expired = manager.prune_stale(max_age=2.0, now=13.1)
        self.assertEqual([item.source_id for item in expired], [screen.source_id])
        self.assertEqual(
            PriorityManager.get_active_source(manager.get_sources()).source_id,
            camera.source_id,
        )

    def test_current_camera_stays_stable_between_equal_priority_sources(self) -> None:
        cameras = [
            source(100, SourceType.CAMERA, seen=1.0),
            source(50, SourceType.CAMERA, seen=1.0),
        ]
        self.assertEqual(
            PriorityManager.get_active_source(cameras, current_active_id=100).source_id,
            100,
        )
        self.assertEqual(PriorityManager.get_active_source(cameras).source_id, 50)

    def test_frame_size_comes_from_native_resolution(self) -> None:
        native = source(
            1,
            SourceType.CAMERA,
            seen=1.0,
            width=640,
            height=480,
        )
        self.assertEqual(native.resolution, (640, 480))
        self.assertEqual(native.expected_frame_size, 640 * 480 * 3 // 2)


class StateMachineTests(unittest.TestCase):
    def test_camera_screen_camera_transition(self) -> None:
        machine = StateMachine()
        self.assertEqual(machine.current_state, RecorderState.WAITING)
        self.assertTrue(machine.transition(RecorderState.CAMERA))
        self.assertTrue(machine.transition(RecorderState.SCREEN_SHARE))
        self.assertTrue(machine.transition(RecorderState.CAMERA))
        self.assertFalse(machine.transition(RecorderState.CAMERA))

    def test_invalid_transition_rejected(self) -> None:
        machine = StateMachine()
        # WAITING -> STOPPED is not allowed
        self.assertFalse(machine.transition(RecorderState.STOPPED))
        self.assertEqual(machine.current_state, RecorderState.WAITING)

    def test_is_recording_property(self) -> None:
        machine = StateMachine()
        self.assertFalse(machine.is_recording)
        machine.transition(RecorderState.CAMERA)
        self.assertTrue(machine.is_recording)
        machine.transition(RecorderState.SCREEN_SHARE)
        self.assertTrue(machine.is_recording)
        machine.transition(RecorderState.STOPPING)
        self.assertFalse(machine.is_recording)


if __name__ == "__main__":
    unittest.main()
