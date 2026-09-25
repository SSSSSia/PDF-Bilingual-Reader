"""翻译任务协作式取消（文献删除联动）：cancel_job 标记测试。"""

from pipeline import processor


def test_cancel_job_sets_flag():
    job_id = "abc123_1700000000"
    processor._jobs[job_id] = {"status": "running", "progress": 10}
    try:
        assert processor.cancel_job(job_id) is True
        assert processor._jobs[job_id]["_cancel"] is True
        # 已置标记后再取消：仍 False（幂等，不重复处理）
        assert processor.cancel_job(job_id) is False
    finally:
        del processor._jobs[job_id]


def test_cancel_job_rejects_non_running():
    processor._jobs["done_job"] = {"status": "done", "progress": 100}
    try:
        assert processor.cancel_job("done_job") is False
        assert "_cancel" not in processor._jobs["done_job"]
    finally:
        del processor._jobs["done_job"]
    # 不存在的任务
    assert processor.cancel_job("no_such_job") is False
