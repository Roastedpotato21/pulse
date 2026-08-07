from unittest.mock import AsyncMock, Mock

import pytest

from pulse.edits import EditProposal, EditResult
from pulse.patch import PatchEngine


@pytest.fixture
def dummy_file(tmp_path):
    file = tmp_path / "dummy.py"
    file.write_text("""
def foo():
    print("foo")

class Bar:
    def __init__(self):
        self.x = 1
""")
    return file

@pytest.fixture
def patch_engine():
    edits = Mock()
    safety_manager = Mock()
    safety_manager.authorize = AsyncMock(return_value=True)
    mutations = Mock()
    mutations.transaction.return_value.__enter__ = Mock()
    mutations.transaction.return_value.__exit__ = Mock()
    
    return PatchEngine(
        edits=edits,
        safety_manager=safety_manager,
        mutations=mutations,
        context_manager=Mock(),
        reasoning_engine=Mock(),
        task_manager=Mock()
    )

def test_locate_node(patch_engine, dummy_file):
    loc_foo = patch_engine.locate_node(dummy_file, "foo")
    assert loc_foo == (2, 3)

    loc_bar = patch_engine.locate_node(dummy_file, "Bar")
    assert loc_bar == (5, 7)
    
    assert patch_engine.locate_node(dummy_file, "nonexistent") is None

import asyncio


def test_apply_patch_replace(patch_engine, dummy_file):
    patch_engine.edits.request_and_apply = AsyncMock()
    patch_engine.edits.request_and_apply.return_value = EditResult(
        proposal=EditProposal(
            file_path=str(dummy_file),
            before_content="",
            after_content="",
            reason="",
            unified_diff=""
        ),
        applied=True
    )
    
    content = "def foo():\n    print('patched')\n"
    
    approve_mock = AsyncMock(return_value=True)
    
    success = asyncio.run(patch_engine.apply_patch(
        file_path=str(dummy_file),
        target_name="foo",
        operation="replace",
        content=content,
        approve=approve_mock
    ))
    
    assert success
    patch_engine.edits.request_and_apply.assert_called_once()
    kwargs = patch_engine.edits.request_and_apply.call_args.kwargs
    modified_content = kwargs["content"]
    assert 'print(\'patched\')' in modified_content
    assert 'print("foo")' not in modified_content

def test_apply_patch_syntax_error(patch_engine, dummy_file):
    # Missing colon creates syntax error
    content = "def foo()\n    print('patched')\n"
    approve_mock = AsyncMock(return_value=True)
    
    with pytest.raises(ValueError, match="Patch would result in invalid Python syntax"):
        asyncio.run(patch_engine.apply_patch(
            file_path=str(dummy_file),
            target_name="foo",
            operation="replace",
            content=content,
            approve=approve_mock
        ))
