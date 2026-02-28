from main import main


def test_smoke(capsys):
    main()
    out = capsys.readouterr().out
    assert "bootstrap ready" in out
