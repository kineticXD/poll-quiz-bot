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

    def test_two_directions_passages_with_ten_un_numbered_questions(self):
        source = """**Directions (01-05):** Study the following information carefully and answer the questions given below.
A and B together can complete a piece of work. A alone can complete the work in 10 days, while B alone can complete the same work in 15 days. Both A and B started working together.

What is the one day's work of A?
A) 1/15
B) 1/12
C) ✅ 1/10
D) 1/6

What is the one day's work of A and B together?
A) 1/5
B) ✅ 1/6
C) 1/12
D) 1/8

In how many days will the work be completed if they work together?
A) 5 days
B) 8 days
C) ✅ 6 days
D) 7 days

If A works alone for 2 days, what fraction of the work is completed?
A) 2/15
B) 1/6
C) ✅ 1/5
D) 1/4

What fraction of the work is left after A works alone for 2 days?
A) 3/4
B) 5/6
C) 2/3
D) ✅ 4/5

**Directions (06-10):** Study the following information carefully and answer the questions given below.
P can complete a task in 6 days, Q can complete the same task in 12 days, and R can complete it in 18 days. They start working together, but P leaves the job after 2 days.

What is the ratio of the efficiencies of P, Q, and R?
A) 3:2:1
B) ✅ 6:3:2
C) 2:3:6
D) 1:2:3

How much work is done by P and Q together in one day?
A) ✅ 1/4
B) 1/3
C) 1/6
D) 1/12

What is the total work done by all three in the first 2 days?
A) 1/3
B) 5/12
C) ✅ 11/18
D) 7/12

After P leaves, in how many days will Q and R finish the remaining work?
A) 3.5 days
B) 3 days
C) ✅ 2.8 days
D) 2.5 days

If all three work together for the entire duration without anyone leaving, how many days will they take to complete the task?
A) 30/11 days
B) 3 days
C) 32/11 days
D) ✅ 36/11 days"""
        questions, warnings, combined_context = parse_structured_quiz(source)
        self.assertEqual(len(questions), 10, warnings)
        self.assertIn("Directions (01-05)", questions[0]["context"])
        self.assertIn("A and B together can complete", questions[0]["context"])
        self.assertIn("Directions (06-10)", questions[5]["context"])
        self.assertIn("P can complete a task in 6 days", questions[5]["context"])
        self.assertIn("A and B together can complete", combined_context)
        self.assertIn("P can complete a task in 6 days", combined_context)
        self.assertEqual([q["correct_id"] for q in questions], [2, 1, 2, 2, 3, 1, 0, 2, 2, 3])
        self.assertTrue(any("expected 5 questions" in warning for warning in warnings) is False, warnings)

    def test_directions_backslash_line_breaks_are_normalized(self):
        source = """**Directions (01-02):** Study the passage.\\
A sample passage gives context.\\
What is the answer?\\
A) No\\
B) ✅ Yes\\
C) Maybe"""
        questions, warnings, _ = parse_structured_quiz(source)
        self.assertEqual(len(questions), 1, warnings)
        self.assertIn("A sample passage", questions[0]["context"])

    def test_directions_range_warns_when_detected_count_is_short(self):
        source = """Directions (01-03): Read this passage.
The passage contains the needed facts.

What is asked here?
A) No
B) ✅ Yes"""
        questions, warnings, _ = parse_structured_quiz(source)
        self.assertEqual(len(questions), 1, warnings)
        self.assertTrue(any("expected 3 questions" in warning and "detected 1" in warning for warning in warnings), warnings)


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
