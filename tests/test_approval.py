from app.approval import APPROVAL_TOKEN, approval_granted, approval_instruction


def test_shared_approval_is_short_case_insensitive_and_explicit():
    assert APPROVAL_TOKEN == "approve"
    assert approval_granted("approve") is True
    assert approval_granted(" APPROVE ") is True
    assert approval_granted(None) is False
    assert approval_granted("") is False
    assert approval_granted("yes") is False
    assert '"approve"' in approval_instruction("run the protected action")
