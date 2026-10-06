"""PyInstaller entry point for the installed release wheel."""

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    from tychora.interfaces.cli import main

    main()
