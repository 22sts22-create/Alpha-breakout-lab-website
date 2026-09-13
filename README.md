Alpha Breakout Lab — Website & Trading Tools

The codebase behind alphabreakoutlab.com — a suite of free, browser-based tools that help traders learn and test strategies before risking real money.

What it is

A live web platform built around a moving-average crossover strategy simulator, plus supporting tools and educational content. It's designed as an honest on-ramp for newer traders: instead of selling stock picks or promising certainty, it teaches people to test strategies and read the results that actually matter.

What it does
Strategy simulator — pick stocks, set fast/mid/slow moving averages, and run a backtest that shows entries, exits, an equity curve, and per-stock results.
Compare mode — test multiple moving-average combinations against one stock to see how much the settings change the outcome.
Performance metrics — reports win rate, drawdown, and (in progress) profit factor and expectancy, so users judge a strategy by the numbers that survive real trading costs, not just headline return.
Glossary — plain-language definitions of trading terms, grouped as a learning path rather than a dictionary.
No signup, free, runs entirely in the browser.
How it works

Built with HTML, CSS, and JavaScript, running client-side in the browser. Accepts uploaded price data (CSV) or simulated price series, computes the moving-average signals and trade results in the browser, and renders the equity curve and trade log directly on the page.

Why I built it

Most trading tools either cost money, require accounts, or hide the metrics that tell you whether a strategy is actually any good. I wanted a clean, free tool that shows the real mechanics — including the parts (costs, drawdown, whether an edge holds up) that most beginner tools gloss over.

Live version

Try it at alphabreakoutlab.com — no account needed.
