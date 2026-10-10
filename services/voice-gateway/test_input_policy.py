import unittest
from input_policy import classify_input,needs_quick_ack


class InputPolicyTests(unittest.TestCase):
    def test_quick_ack_only_for_direct_requests(self):
        for text in ('帮我查一下长期训练任务','那你查一下','请查询昨天的消息','帮我查'):
            self.assertTrue(needs_quick_ack(text),text)
        for text in ('不用帮我查','他说帮我查一下','帮我查是什么意思','如果你帮我查会怎样','帮我查一下，不用现在查','你好','等一下'):
            self.assertFalse(needs_quick_ack(text),text)
    def test_clear_stop_commands(self):
        for text in ('等一下','等等','请先停一下','你先别说了','停下吧','不要读了','诶，等一下，等一下等一下','停停停','呃，请先停一下'):
            with self.subTest(text=text):self.assertEqual(classify_input(text),'stop')

    def test_pure_acknowledgements(self):
        for text in ('嗯','嗯嗯。','对对对','是的','没错','好的','OK'):
            with self.subTest(text=text):self.assertEqual(classify_input(text),'backchannel')

    def test_substantive_input_and_negations_are_not_acknowledgements(self):
        for text in ('对，但是我想问另一件事','好，先别继续','别停','不要停下',
                     '他说等一下是什么意思','我不想停止任务','取消刚才的任务','继续详细说'):
            with self.subTest(text=text):self.assertEqual(classify_input(text),'redirect')

    def test_empty_is_not_a_new_question(self):
        for text in ('','  ',None):self.assertEqual(classify_input(text),'pending')



from input_policy import explicit_campfire_lookup
for text in ('你查一下篝火有什么新消息吗？','查询下篝火消息','帮我查篝火最新消息','请查询篝火的实时最新消息'):
    assert explicit_campfire_lookup(text),text
for text in ('不要查询篝火消息','他说查一下篝火消息','怎么查询篝火消息','查一下篝火消息然后发给他','查一下它','篝火是什么','查一下私信消息'):
    assert not explicit_campfire_lookup(text),text

if __name__=='__main__':unittest.main()
