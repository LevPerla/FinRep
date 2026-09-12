import subprocess
import sys
import textwrap


def test_importing_transaction_reader_does_not_hide_python_warnings():
    script = textwrap.dedent(
        """
        import warnings

        warnings.resetwarnings()
        warnings.simplefilter("always")
        import src.data.get

        with warnings.catch_warnings(record=True) as caught:
            warnings.warn("warning-policy-sentinel", UserWarning)

        assert len(caught) == 1, warnings.filters
        assert str(caught[0].message) == "warning-policy-sentinel"
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
