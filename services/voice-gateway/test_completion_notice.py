import unittest
from completion_notice import completion_notice

class CompletionTests(unittest.TestCase):
    def test_final_result_wins_over_old_progress(self):
        body='现在停止服务。GPU0进程还在。正在核实父进程。**两句结论**：服务已停止，显存已释放。端口不再监听。\n细节：先停止子进程。'
        result=completion_notice(body)
        self.assertIn('服务已停止',result);self.assertIn('端口不再监听',result)
        self.assertNotIn('进程还在',result);self.assertNotIn('细节',result)
    def test_task_completion_is_not_operation_success(self):
        result=completion_notice('准备停止服务。最终结果：权限不足，未停止服务。')
        self.assertIn('未停止服务',result)
    def test_no_final_section_notifies_without_claiming_old_progress(self):
        result=completion_notice('进程还在。准备继续检查。')
        self.assertIn('已返回完整结果',result);self.assertNotIn('进程还在',result)

if __name__=='__main__':unittest.main()
