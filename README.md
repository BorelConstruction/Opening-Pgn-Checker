# Opening Prep Tools

A toolkit for analyzing and improving chess opening PGN files, with optional visualization of move relationships.

## Overview

Features:

   - Detects gaps in an opening repertoire
   - Generates candidate continuations
   - Annotates PGN with NAGs and color highlights
   - Builds a graph of move relationships from a position using a pgn or database (experimental)

The application is launched via a GUI or CLI.

---

## Entry Point

Main GUI:
gui/main.py

---

## Features

### 1. PGN Checker

**Input:**
- PGN file
- Lichess API token
- Chess engine path

**Output:**
- A new PGN written to output_pgns/

**Annotation meaning**:
- Red: a very popular opponent response
- Yellow: Common but not very popular opponent response
- Green: Our move that is uncommon
- Zugzwang: A move that is important to memorize
- Zeitnot: read as “don't confuse!”
- Development advantage: “safe to confuse, our responses are all the same”

### 2. Graph Builder

**Input:**
- The starting position of an opening
- Lichess API token
- A PGN if a user wants it to be file-based

**Output:**

A graph of move dependencies in the opening


---

## Requirements

- Python 3.x
- Stockfish
- For most use cases – Internet connection

Python dependencies:
- lichess
- chess

---

## Usage

1. Launch the GUI:

python -m source.gui.main

2. Choose a feature:
- PGN Checker
- Move Graph Builder

3. Provide, if not cached:
- Engine path
- Lichess API token
- PGN file and/or position
- Select options if desired



## Use case examples

- I have an opening file deemed complete, and I use the checker to verify I didn't miss anything. I may or may not go for auto-suggestions.
- I have an opening line idea and I want to quickly complete it to an opening file.
- I want to mark moves in my files to aid memorization and add structure to them.
- I want to find patterns in my files and improve them (this is being developed).


## Implementation Details

- Results of heavy operations are cached.
- Gaps are moves not mentioned in pgn which are sufficiently good/popular (choice function is complex and evolving).
- Move choice is based on engine evaluation, move score rate, chances to transpose and (in progress) presumed ease of memorization.

---

## Motivation

1. Making opening files is hard work, and the tool is hoped to save 50+% of time.
2. In a highbrower way, I am curious about finding structure underlying data. In particular, I believe that the pgn tree memorization can be greatly aided by explicating patterns found within this tree. Teasing as many of these as possible is a long-term goal. I can see of a dozen ways to be smart about opening lines, it would be interesting to see which are possible to implement.

Other long-term ideas include a memorization tool – I don't find widely available spaced repetition tools like that of Chessable adequate.
