import unittest
from voice_notices import acknowledgement,submitted_notice,ACKNOWLEDGEMENTS

class NoticeTests(unittest.TestCase):
    def test_acknowledgements_do_not_repeat_immediately(self):
        for previous in ACKNOWLEDGEMENTS:
            for _ in range(12):
                value=acknowledgement(previous)
                self.assertIn(value,ACKNOWLEDGEMENTS);self.assertNotEqual(value,previous)
    def test_submission_names_task_without_claiming_execution_success(self):
        text=submitted_notice('停止Qwen评测服务')
        self.assertIn('停止Qwen评测服务',text);self.assertIn('我正在查',text)
        self.assertNotIn('已经停止',text)

if __name__=='__main__':unittest.main()
