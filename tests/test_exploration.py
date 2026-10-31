import json
from unittest.mock import Mock

from test_next_stage import Fixture


class ExplorationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.entry.getPlayersInfo = lambda n: [{"name": "JokerBot", "distance": "0", "position": "10, 64, 10"},
                                               {"name": "AlexPlayer", "distance": "2"}]
        self.entry.getSurfaceMap = lambda r: [{"dx": "2", "dz": "1", "top_dy": "-1", "block": "grass block"}]
        self.entry.gotoXYZ = Mock(return_value={"ok": True})
        self.entry.pathStatus = Mock(return_value={"ok": True, "goal": "14,64,10", "arrived": False})

    def test_observations_include_actual_coordinates_and_surface(self):
        data = self.app.minecraft.observe()
        self.assertEqual(data["self"]["position"], [10, 64, 10])
        self.assertEqual(data["surface_map"], [{"x": 12, "y": 64, "z": 11, "ground": "grass block"}])

    def test_walk_is_not_complete_until_native_arrival_and_idle(self):
        result = self.app.minecraft.execute("walk", x=14, y=64, z=10)
        self.assertFalse(result["completed"])
        self.entry.gotoXYZ.assert_called_once_with(14, 64, 10)
        self.entry.active = True
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "running")
        self.entry.pathStatus.return_value.update(arrived=True)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "running")
        self.entry.active = False
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "arrived")

    def test_invalid_or_far_points_do_not_reach_navigation(self):
        for x, y, z in ((True, 64, 10), (14, -100, 10), (30000001, 64, 10)):
            with self.assertRaises(ValueError):
                self.app.minecraft.execute("walk", x=x, y=y, z=z)
        self.assertFalse(self.app.minecraft.execute("walk", x=100, y=64, z=10)["accepted"])
        self.assertFalse(self.app.minecraft.execute("walk", x=12, y=80, z=10)["accepted"])
        self.entry.gotoXYZ.assert_not_called()

    def test_native_timeout_is_not_reported_as_arrival(self):
        self.app.minecraft.execute("walk", x=14, y=64, z=10)
        self.entry.pathStatus.return_value = {"ok": False, "reason": "client thread timeout"}
        self.app.minecraft.refresh()
        self.assertNotEqual(self.app.minecraft.task["state"], "arrived")

    def test_native_busy_false_is_not_treated_as_immediate_path_failure(self):
        self.app.minecraft.execute("walk", x=14, y=64, z=10)
        self.app.minecraft.task["started_at"] -= 4
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "running")
        self.assertEqual(self.entry.stop_calls, 0)
        self.app.minecraft.task["last_progress_at"] -= 9
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.stop_calls, 1)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "not_arrived")

    def test_other_navigation_goal_is_not_misreported_as_this_arrival(self):
        self.app.minecraft.execute("walk", x=14, y=64, z=10)
        self.app.minecraft.task["started_at"] -= 4
        self.entry.pathStatus.return_value.update(goal="90,64,90", arrived=True)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")

    def test_autonomy_can_choose_exploration_without_people_nearby(self):
        self.app.handle("/цель Исследуй мир")
        self.app.handle("/автономность вкл")
        self.model.decision.update(action="walk", x=14, y=64, z=10)
        self.app.runtime.tick()
        self.entry.gotoXYZ.assert_called_once_with(14, 64, 10)
        data = json.loads(self.model.context)[0]["content"]
        self.assertIn("surface_map", data)

    def test_mask_change_during_planning_cancels_old_public_identity(self):
        self.app.handle("/цель Исследуй мир и знакомься")
        self.app.handle("/автономность вкл")
        original = self.model.generate
        def switch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.app.switch("engineer")
            return result
        self.model.generate = switch
        self.model.decision.update(action="say", message="Я Саныч.")
        self.app.runtime.tick()
        self.assertEqual(self.app.active, "engineer")
        self.assertEqual(self.entry.chats, [])
        event = self.app.experience.recent("world:minecraft")[-1]
        self.assertEqual(event["kind"], "persona.changed")
