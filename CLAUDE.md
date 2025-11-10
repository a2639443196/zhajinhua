# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a sophisticated multiplayer "Zhajinhua" (炸金花) card game implementation with AI-powered players, featuring web-based real-time gameplay, advanced game mechanics, and comprehensive player systems.

## Core Architecture

### Main Components

- **`zhajinhua.py`** - Core game engine with `ZhajinhuaGame` class managing game state, rules, and flow
- **`game_controller.py`** - High-level game controller coordinating players, handling special systems (vault/loans, items, auctions)
- **`player.py`** - AI player implementation with LLM integration, persona system, experience tracking
- **`game_rules.py`** - Card game rules, hand evaluation, and core data structures
- **`server.py`** - FastAPI web server with WebSocket support for real-time gameplay
- **`llm_client.py`** - LLM client for AI player decision-making

### Key Systems

**Player AI System:**
- LLM-powered decision making using various models (Kimi, Qwen, DeepSeek, etc.)
- Persona-based playing styles with experience tracking
- Pressure calculations and psychological elements
- Cheat detection and mindgame mechanics

**Game Mechanics:**
- Standard Zhajinhua rules with hand rankings (High Card → Pair → Straight → Flush → Straight Flush → Trips → Special 235)
- Betting, raising, comparing cards, and showdown mechanics
- Special actions: accusations, bribes, auction bids
- Loan system with collateral based on hand strength

**Data Persistence:**
- `items_store.json` - Game items and power-ups
- `used_personas.json` - Tracks used player personas to prevent repetition

## Running the Game

### Development Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Run the web server
python server.py
```

### Configuration
- Modify `server.py` for player configurations and auto-shutdown settings
- Game rules and mechanics can be adjusted in `game_rules.py`
- Player AI behavior controlled through prompt templates in `prompt/` directory

## Development Commands

### Running Tests
```bash
# Test LLM client functionality
python test_llm_stream.py

# Test game logic (create test files as needed)
python -m pytest tests/  # if pytest tests exist
```

### Key Files for Modification
- **Game balance:** `game_rules.py` (hand values, betting costs)
- **Player behavior:** `prompt/decide_action_prompt.txt`, `prompt/reflect_prompt_template.txt`
- **New features:** `game_controller.py` (special mechanics)
- **Web interface:** `server.py` (endpoints, WebSocket handling)

## Prompt System

The AI players use structured prompts from the `prompt/` directory:
- `decide_action_prompt.txt` - Main action decision logic
- `reflect_prompt_template.txt` - Post-action reflection and learning
- `defend_prompt.txt` - Defense against accusations
- `vote_prompt.txt` - Voting mechanics
- `create_persona_prompt.txt` - Character personality generation

## Important Implementation Notes

### Game State Management
- Game state is centrally managed in `ZhajinhuaGame.state`
- Player actions are validated through `available_actions()` method
- Event system allows for extensible game mechanics

### AI Player Logic
- Players maintain pressure snapshots based on chip ratios
- Experience system affects loan eligibility and betting behavior
- Persona tags (aggressive, cautious, deceptive) influence decision patterns

### Loan and Vault System
- Players can request loans based on experience and hand strength
- Interest rates and collateral calculations in `SystemVault` class
- Loan data tracked per player with repayment schedules

### Web Server Features
- Real-time gameplay via WebSocket connections
- Game log collection and download functionality
- Auto-shutdown when no spectators are present
- Player streaming and observer mode support