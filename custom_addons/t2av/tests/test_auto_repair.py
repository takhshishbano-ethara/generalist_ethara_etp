from odoo.tests.common import BaseCase, tagged

from odoo.addons.t2av.services import auto_repair, validator as validator_svc


def _validate(text, *, style="precise", category=""):
    report = validator_svc.validate(text, style=style, category=category)
    return validator_svc.categorize(report), report


@tagged("post_install", "-at_install", "t2av")
class TestAutoRepair(BaseCase):

    def test_drone_hardware_rewritten(self):
        text = (
            "A tiger drinks water at a river. The drone follows the tiger as it "
            "leans forward. Slow handheld arc captures every detail. "
            "Audio: water lapping, leaves rustling, distant bird calls. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="animals_wildlife", style="precise",
        )
        self.assertIn("hardware:drone", applied)
        self.assertNotIn("drone", repaired.lower())
        bucket, report = _validate(repaired, category="animals_wildlife")
        self.assertNotEqual(bucket, "fatal", report.to_dict())

    def test_aerial_hardware_rewritten(self):
        text = (
            "An aerial view of the canyon at sunrise. The hawk soars while a "
            "ranger walks the ridge. Static wide of the valley floor. "
            "Audio: wind, distant calls, footsteps on gravel. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="nature_landscape", style="precise",
        )
        self.assertTrue(any(a.startswith("hardware:") for a in applied), applied)
        self.assertNotIn("aerial", repaired.lower())
        bucket, _r = _validate(repaired, category="nature_landscape")
        self.assertNotEqual(bucket, "fatal")

    def test_multi_shot_cut_to_collapsed(self):
        text = (
            "A chef kneads dough. Cuts to a close-up of the rising loaf. "
            "Static medium of the kitchen. "
            "Audio: hands on dough, oven hum, soft footsteps. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="food", style="precise",
        )
        self.assertTrue(any(a.startswith("multi_shot:") for a in applied), applied)
        self.assertNotIn("cuts to", repaired.lower())
        bucket, _r = _validate(repaired, category="food")
        self.assertNotEqual(bucket, "fatal")

    def test_brand_leak_stripped(self):
        text = (
            "A model walks down a Times Square sidewalk wearing Nike. "
            "Static medium of the storefront. "
            "Audio: footsteps, traffic, distant chatter. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="fashion", style="precise",
        )
        self.assertTrue(any(a.startswith("brand:") for a in applied), applied)
        lower = repaired.lower()
        self.assertNotIn("times square", lower)
        self.assertNotIn("nike", lower)
        bucket, _r = _validate(repaired, category="fashion")
        self.assertNotEqual(bucket, "fatal")

    def test_marketing_words_stripped(self):
        text = (
            "A breathtaking, stunning view of the falls — majestic and ethereal. "
            "A guide walks the trail. Static wide of the gorge. "
            "Audio: roaring water, footsteps, distant wind. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="nature_landscape", style="precise",
        )
        self.assertTrue(any(a.startswith("marketing:") for a in applied), applied)
        self.assertNotIn("breathtaking", repaired.lower())
        self.assertNotIn("stunning", repaired.lower())
        self.assertNotIn("ethereal", repaired.lower())
        bucket, _r = _validate(repaired, category="nature_landscape")
        self.assertNotEqual(bucket, "fatal")

    def test_ai_tell_em_dash_normalised(self):
        text = (
            "A barista pours espresso \u2014 the crema rises slowly. "
            "Static close-up of the cup. "
            "Audio: steam wand, milk frothing, soft music. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="food", style="precise",
        )
        self.assertIn("ai_tell:em-dash", applied)
        self.assertNotIn("\u2014", repaired)
        bucket, _r = _validate(repaired, category="food")
        self.assertNotEqual(bucket, "fatal")

    def test_decimal_timestamp_removed(self):
        text = (
            "A sprinter accelerates and then turns sharply at t=2.3s. "
            "Static medium of the track. "
            "Audio: footsteps, breathing, distant crowd. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="sports", style="precise",
        )
        self.assertTrue(any(a.startswith("decimal_ts:") for a in applied), applied)
        self.assertNotIn("t=2.3s", repaired)
        bucket, _r = _validate(repaired, category="sports")
        self.assertNotEqual(bucket, "fatal")

    def test_forbidden_resolution_removed_but_suffix_preserved(self):
        text = (
            "A driver leans into the curve with 4K clarity. "
            "Static medium of the dashboard. "
            "Audio: engine, tires, wind. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="cars", style="precise",
        )
        self.assertTrue(any(a.startswith("forbidden_res:") for a in applied), applied)
        self.assertNotIn("4K", repaired)
        self.assertIn("1920x1080", repaired)
        self.assertIn(auto_repair.MANDATORY_SUFFIX_STEREO, repaired)
        bucket, _r = _validate(repaired, category="cars")
        self.assertNotEqual(bucket, "fatal")

    def test_multiple_camera_moves_collapsed_to_one(self):
        text = (
            "A chef prepares dough. Slow push-in on flour dust rising. "
            "Then slow pull-out reveals the entire kitchen. "
            "The dough rolls forward as she folds it twice. "
            "Audio: hands on dough, oven hum, soft footsteps. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="food", style="precise",
        )
        self.assertTrue(
            any(a.startswith("camera_move_extra:") for a in applied), applied,
        )
        bucket, _r = _validate(repaired, category="food")
        self.assertNotEqual(bucket, "fatal")

    def test_missing_audio_block_injected(self):
        text = (
            "A chef kneads dough with practiced hands. "
            "Slow push-in on flour dust rising from the wooden board. "
            "The dough rolls forward as she folds it twice. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="food", style="precise",
        )
        self.assertTrue(any(a.startswith("audio:") for a in applied), applied)
        self.assertIn("Audio:", repaired)
        bucket, _r = _validate(repaired, category="food")
        self.assertNotEqual(bucket, "fatal")

    def test_kitchen_sink_combination(self):
        text = (
            "A majestic tiger drinks water at a Disney river in Hollywood. "
            "The drone captures a stunning aerial view as the tiger leans forward. "
            "Cuts to a Nike-branded close-up of the water surface. "
            "The slow handheld arc follows the breathtaking scene with anamorphic flair. "
            "Audio: water lapping, leaves rustling, distant birds. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="animals_wildlife", style="precise",
        )
        self.assertGreaterEqual(len(applied), 5, applied)
        lower = repaired.lower()
        for forbidden in ("drone", "aerial", "disney", "nike", "anamorphic",
                          "majestic", "stunning", "breathtaking", "cuts to"):
            self.assertNotIn(forbidden, lower, f"{forbidden!r} survived repair")
        bucket, _r = _validate(repaired, category="animals_wildlife")
        self.assertNotEqual(bucket, "fatal")

    def test_word_count_high_trimmed_into_band(self):
        body = (
            "A potter shapes wet clay on a slowly spinning wheel near a window. "
            "Slow handheld arc follows the potter as she leans forward. "
            "Audio: wet clay whirring, footsteps on wood, rain on tile. "
        )
        filler = (
            "The amber light is bright against the rough cedar planks of the wide floor. "
            "Outside, the air is heavy under a fading evening sky of dusty pink. "
            "The yellow paint on the wall is weathered into pale pastel patches over time. "
            "A brass lantern is on a wooden table near the studio window of clear glass. "
            "The mood throughout is patient and deeply local in feeling and texture. "
            "The corners are full of long shadows where the afternoon light is absent. "
            "A frayed grey curtain is loose against the chalk plaster wall of pale tone. "
            "The floorboards are marked from long years of harsh weather and frequent footsteps. "
            "Tones throughout the small room are earthy, ochre, and unhurried in feeling. "
            "Everything in the frame is contained inside the small workshop space. "
            "The texture of the wood is rich and bronze under the lantern light. "
            "Dust is suspended in the heavy air of the workshop. "
            "The window glass is misted at the edges from the damp evening. "
            "Brown and ochre are dominant in every surface of this small lived-in space. "
            "The bench is loaded with tools and clay pellets ready for the next batch. "
            "Behind the wheel is a tall wooden shelf of finished bowls and cups. "
            "Smoke is rising in a thin column from a clay kiln in the back yard. "
        )
        text = body + filler + auto_repair.MANDATORY_SUFFIX_STEREO
        repaired, applied = auto_repair.repair_all(
            text, category="indoor_lifestyle", style="precise",
        )
        self.assertTrue(
            any(a.startswith("word_count_high:") for a in applied),
            f"expected word_count_high tag in {applied}",
        )
        bucket, report = _validate(repaired, style="precise", category="indoor_lifestyle")
        self.assertNotEqual(bucket, "fatal", report.to_dict())
        for f in (report.fatal + report.warnings):
            self.assertNotIn(f.rule, ("word_count.high", "word_count.runaway"))

    def test_word_count_low_padded_into_band(self):
        text = (
            "A potter shapes wet clay on a slowly spinning wheel. "
            "Slow handheld arc follows the potter as she leans forward. "
            "Audio: wet clay whirring, soft footsteps, distant rain on tile. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        repaired, applied = auto_repair.repair_all(
            text, category="indoor_lifestyle", style="precise",
        )
        self.assertTrue(
            any(a.startswith("word_count_low:") for a in applied),
            f"expected word_count_low tag in {applied}",
        )
        bucket, report = _validate(repaired, style="precise", category="indoor_lifestyle")
        self.assertNotEqual(bucket, "fatal", report.to_dict())
        for f in (report.fatal + report.warnings):
            self.assertNotIn(f.rule, ("word_count.low",))

    def test_idempotent_clean_text(self):
        text = (
            "A tiger walks slowly through tall grass at a riverbank. "
            "Static wide of the open clearing. "
            "Audio: rustling grass, distant bird calls, soft wind. "
            + auto_repair.MANDATORY_SUFFIX_STEREO
        )
        bucket, _r = _validate(text, category="animals_wildlife")
        if bucket == "clean":
            repaired, applied = auto_repair.repair_all(
                text, category="animals_wildlife", style="precise",
            )
            self.assertEqual(applied, [])
            self.assertEqual(repaired, text)
