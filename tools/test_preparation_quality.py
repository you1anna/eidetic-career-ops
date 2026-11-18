"""Independent negative controls: valid JSON and clean style do not establish truth."""
import unittest

from test_weekly_preparation import PreparationCase, FakeCodex, good_message
import approach_preparation as prep
import preparation_wording as wording


class GroundingTests(PreparationCase):
    def test_prepared_note_numbers_the_shapes_and_bolds_the_ask(self):
        import search_config
        import note_context
        pack = search_config.templates()
        packet = {'relationship': {'your_note': 'Worked with him at Northwind.'},
                  'ask': {'kind': 'catch_up'}, 'person': {'first_name': 'Sam'},
                  'cv': {'lead_with': 'leading an engineering team'}}
        message = wording.options(packet, pack)[0]['message']
        self.assertIn('1. leading an engineering team\n2. building AI products hands-on\n'
                      "3. coming into a system that's struggling and fixing how it ships", message)
        self.assertIn('**Would you be up for a 30-minute call', message)
        self.assertNotRegex(message, r'(?m)^[ \t]*[-*•][ \t]')
        self.assertEqual(note_context.lint_note(message, template='warm', pack=pack), [])

    def test_shorthand_conversions_preserve_subject_and_fact(self):
        examples = {
            'worked with him at Northwind, good colleague': 'I worked with you at Northwind.',
            'good colleague, worked with her at Birch': 'I worked with you at Birch.',
            'used to work for him at Maple': 'I used to work for you at Maple.',
            'he recruited me into Cedar': 'You recruited me into Cedar.',
            'worked in her team for years at Elm': 'I worked in your team at Elm.',
            'worked for him at Maple': 'I worked for you at Maple.',
            'worked with him at Northwind and later moved to Birch': 'I worked with you at Northwind and later moved to Birch.',
            'worked in his team for years at Elm and then left': 'I worked in your team at Elm and then left.',
        }
        for source, expected in examples.items():
            with self.subTest(source=source):
                line, quote, _ = wording.personal_lines(source)[0]
                self.assertEqual(line, expected)
                self.assertIn(quote, source)
        for source in ('Sam worked with him at Northwind.', 'never worked with him at Northwind',
                       'worked with him at Northwind but I may be confusing him with Sam',
                       'maybe worked with him at Northwind', 'I nearly hired him into Northwind',
                       'Sam led the project, worked with him at Northwind.', 'worked with him at Northwind?',
                       'worked with him at Northwind, if I remember correctly'):
            with self.subTest(source=source):
                self.assertEqual(wording.personal_lines(source), [])

    def test_any_retained_wording_is_read_only_even_if_supported(self):
        before=list(self.conn.iterdump())
        for text in ('A supported saved note', 'You offered me 15 engineering jobs'):
            result=prep.apply(self.conn,{},dict(message=text),'research:historical')
            self.assertEqual(result['outcome'],'retired')
        self.assertEqual(list(self.conn.iterdump()),before)


if __name__ == '__main__':
    unittest.main()
