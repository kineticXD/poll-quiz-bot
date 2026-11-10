import unittest

from quiz_engine import parse_structured_quiz, validate_ai_quiz, generate_extractable_questions


class StructuredParserTests(unittest.TestCase):
    def test_basic_checkmark_format(self):
        source = """What is 2+2?
a) 3
b) 4 ✅
c) 5
Ex: Basic math"""
        questions, warnings, context = parse_structured_quiz(source)
        self.assertEqual(len(questions), 1, warnings)
        self.assertEqual(questions[0]["correct_id"], 1)
        self.assertEqual(questions[0]["explanation"], "Basic math")

    def test_answer_key_and_multiline_explanation(self):
        source = """1. Which planet is known as the Red Planet?
A) Venus
B) Mars
C) Jupiter
D) Saturn
Answer: B
Explanation: Mars appears reddish because of iron oxide.
It is often called the Red Planet."""
        questions, warnings, _ = parse_structured_quiz(source)
        self.assertEqual(len(questions), 1, warnings)
        self.assertEqual(questions[0]["correct_id"], 1)
        self.assertIn("iron oxide", questions[0]["explanation"])

    def test_shared_context_and_multiple_questions(self):
        source = """Read the passage and answer the questions.
The passage gives general background for the following items.

Q1. What is 2+2?
(A) 3
(B) 4 ✔️
(C) 5

Q2: Which number is even?
a. 3
b. 5
c. 8 [correct]
d. 9"""
        questions, warnings, context = parse_structured_quiz(source)
        self.assertEqual(len(questions), 2, warnings)
        self.assertIn("general background", context)
        self.assertEqual(questions[0]["correct_id"], 1)
        self.assertEqual(questions[1]["correct_id"], 2)
        self.assertIn("general background", questions[0]["context"])

    def test_unmarked_question_is_rejected(self):
        source = """What is 2+2?
a) 3
b) 4
c) 5"""
        questions, warnings, _ = parse_structured_quiz(source)
        self.assertEqual(questions, [])
        self.assertTrue(any("no clear correct answer" in w for w in warnings))


class ParagraphValidationTests(unittest.TestCase):
    def test_ai_answer_must_be_source_grounded(self):
        source = "The National Sample Survey (NSS) is conducted in India."
        valid = {
            "title": "Test",
            "questions": [{
                "question": "What does NSS stand for?",
                "options": ["National Sample Survey", "National Social Study", "National Sample System", "National Statistics Service"],
                "answer": "National Sample Survey",
                "evidence": "The National Sample Survey (NSS) is conducted in India.",
                "explanation": "The paragraph gives the full form in parentheses.",
            }],
        }
        self.assertEqual(len(validate_ai_quiz(valid, source)), 1)
        valid["questions"][0]["evidence"] = "A fabricated sentence."
        with self.assertRaises(ValueError):
            validate_ai_quiz(valid, source)

    def test_offline_extractor_handles_number_words_and_dates(self):
        source = "The United Nations was established on 24 October 1945. It has six principal organs."
        questions = generate_extractable_questions(source)
        self.assertTrue(any("When was the United Nations established?" == q["question"] for q in questions))
        count_question = next(q for q in questions if "how many" in q["question"].lower())
        self.assertIn("six", [x.lower() for x in count_question["options"]])
        self.assertEqual(len(count_question["options"]), 4)
        self.assertIn("united nations", count_question["question"].lower())
        evidence_items = [q.get("evidence") for q in questions]
        self.assertEqual(len(evidence_items), len(set(evidence_items)))

    def test_offline_extractor_does_not_invent_unstructured_items(self):
        source = "This is a reflective paragraph about curiosity and the value of asking questions."
        self.assertEqual(generate_extractable_questions(source), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
