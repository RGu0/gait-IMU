def test_deliberately_failing():
    """一次性：验证 CI 在测试失败时确实变红。不会合并。"""
    assert False, "red path probe"
