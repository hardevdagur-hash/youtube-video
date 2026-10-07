"""Unit tests for EnglishConverter."""

from services.english_converter import english_converter


class TestEnglishConverter:
    """Test suite verifying all English (India) conversion requirements."""

    def test_asr_mishearing_corrections(self):
        """Verify Whisper ASR mishearings are corrected to canonical Indian academic terms."""
        assert "JEE Advanced" in english_converter.correct_asr_errors("Student is preparing for j-advans exam")
        assert "JEE Main" in english_converter.correct_asr_errors("Score high in jymyn session 1")
        assert "IIT Delhi" in english_converter.correct_asr_errors("He got mechanical branch in iti daily")
        assert "NCERT" in english_converter.correct_asr_errors("Read chemistry from ncrt thoroughly")
        assert "GPT" in english_converter.correct_asr_errors("Use gpt to generate practice problems")
        assert "ChatGPT" in english_converter.correct_asr_errors("Ask chat-gpt for assistance")
        assert "LeetCode" in english_converter.correct_asr_errors("Solve 200 problems on lid kod")

    def test_repetition_removal(self):
        """Verify teacher stutters and repeated phrases are cleaned."""
        # Known teacher repetition
        cleaned = english_converter.convert("Physics par. Physics par. Physics par.")
        assert "Focus on Physics." in cleaned

        cleaned_q = english_converter.convert("question ko question ko solve karo")
        assert "Solve the question." in cleaned_q

        # Duplicate short phrases
        assert english_converter.remove_repetitions("Focus on Physics. Focus on Physics.").strip().startswith("Focus on Physics")

    def test_numbers_and_ranges(self):
        """Verify numbers, percentiles, and strategy formats."""
        assert "96–97 percentile" in english_converter.format_numbers_and_ranges("target 96 97 percentile")
        assert "130+" in english_converter.format_numbers_and_ranges("aim for 130 plus in physics")
        assert "35–45 marks" in english_converter.format_numbers_and_ranges("score 35 to 45 marks in section 1")
        assert "50-30-20 strategy" in english_converter.format_numbers_and_ranges("follow this 50 30 20 strategy")

    def test_educational_phrases_conversion(self):
        """Verify spoken Hindi educational expressions convert to natural Indian English."""
        res1 = english_converter.convert("JEE Advanced ki tayari karo")
        assert "Prepare for JEE Advanced" in res1 or "prepare for JEE Advanced" in res1

        res2 = english_converter.convert("JEE exam do with full confidence")
        assert "Give the JEE exam" in res2 or "give the JEE exam" in res2

        res3 = english_converter.convert("paper attempt karo systematically")
        assert "Attempt the paper" in res3 or "attempt the paper" in res3

        res4 = english_converter.convert("question ko dobara solve karo aur apni galtiyon ko note karo")
        assert "Solve the question again and note down your mistakes." in res4

    def test_technical_terms_preservation(self):
        """Ensure educational and technical terms are preserved with canonical casing."""
        input_text = "JEE Advanced, JEE Main, IIT Delhi, IIT, NIT, NEET, NCERT, GPT, percentile, marks, rank, score, strategy, syllabus, revision, backlog, question, paper"
        converted = english_converter.convert(input_text)
        for term in ["JEE Advanced", "JEE Main", "IIT Delhi", "IIT", "NIT", "NEET", "NCERT", "GPT"]:
            assert term in converted

    def test_full_text_preservation_no_summarization(self):
        """Ensure full transcript is preserved without truncation or summary."""
        long_text = (
            "Step 1: First clear the backlog in mathematics. "
            "Step 2: Read NCERT line by line for chemistry. "
            "Step 3: Aim for 130 plus marks in Physics. "
            "Step 4: Target 96 97 percentile in JEE Main. "
            "Step 5: Question ko dobara solve karo aur apni galtiyon ko note karo."
        )
        result = english_converter.convert(long_text)
        assert "clear the backlog" in result
        assert "NCERT" in result
        assert "130+" in result
        assert "96–97 percentile" in result
        assert "JEE Main" in result
        assert "Solve the question again and note down your mistakes." in result
        # Check that all 5 steps are present
        assert "Step 1" in result and "Step 5" in result

    def test_semantic_asr_reconstructions(self):
        """Verify semantic ASR mishearings are reconstructed to natural Indian English."""
        raw = (
            "In 2006, the Adi Tech Industry is telling you that the lead code is time wasted "
            "and the Adi Industry is telling you to hire one of these people. "
            "Both of you are taking care of the failure. "
            "Because we live in a seara where 40% boilerplate can be written. "
            "Top startups of the Y are hiring. "
            "Wipe coding is dangerous. Handling a jeera ticket. They need an AI or a stator."
        )
        converted = english_converter.convert(raw)
        assert "In 2026, half of the tech industry" in converted
        assert "LeetCode is a waste of time" in converted
        assert "Both are setting you up for failure" in converted
        assert "era" in converted
        assert "boilerplate code can be written" in converted
        assert "Y Combinator" in converted
        assert "Vibe coding" in converted or "vibe coding" in converted.lower()
        assert "Jira ticket" in converted
        assert "AI orchestrator" in converted

    def test_title_aware_entity_resolution(self):
        """Verify video title provides dynamic context to disambiguate series and show names."""
        raw = "Welcome everyone, our S1 question is equal to IIT selection series."
        title = "1 Que = IIT Selection | Episode 4 for JEE Advanced Mechanics"
        converted = english_converter.convert(raw, title=title)
        assert "1 Que = IIT Selection series" in converted

        raw_pg = "Check the fuzzic galaxy advanced illustration book."
        converted_pg = english_converter.convert(raw_pg, title="Physics Galaxy Mechanics", channel="Physics Galaxy")
        assert "Physics Galaxy" in converted_pg
        assert "Advanced Illustrations book" in converted_pg

    def test_transcript_quality_validation(self):
        """Verify the quality validator checks language, preservation, and non-roman script."""
        raw = "Target 96 97 percentile and score 130 plus in Physics."
        clean = english_converter.convert(raw)
        val = english_converter.validate_transcript(clean, raw)
        assert val["is_valid"] is True
        assert val["language"] == "English (India)"
        assert val["no_non_roman_script"] is True
        assert val["length_ratio"] >= 0.70
        assert val["numbers_preserved"] is True
