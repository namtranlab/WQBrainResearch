# How to run

1. Put your BRAIN login in `WQBrainSim/.env`:
   ```dotenv
   BRAIN_USERNAME=your_email
   BRAIN_PASSWORD=your_password
   ```
2. Start:
   ```bash
   ./autorun.sh
   ```

It runs by itself: Claude writes ideas, the script tests them, and it submits up to 3 passing alphas a day. Submissions cannot be undone.

- Keep the terminal open and the laptop lid open. The script keeps the Mac awake, but closing either one stops it.
- Stop: press Ctrl-C. Run `./autorun.sh` again to continue where it stopped.
- Results: `results/research.log`.
- It recovers by itself from temporary problems. For a problem it does not recognise, Claude picks a safe next step.
- When you must act, for example to log in to BRAIN, it stops and shows a Mac notification. The message says what to do.

## Author

Nam Tran (vannam.tran@ntu.edu.sg)

## License

MIT. See `LICENSE`.
