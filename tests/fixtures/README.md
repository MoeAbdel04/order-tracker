# Sample emails

Each `.eml` file is one order email the tests feed through the scanner.

They are **not real emails**. The Target, Amazon, Mattel and Pokémon Center files are rebuilt by
hand from the layout of real order emails: same subjects, same wording, same order of lines and
table cells, with the marketing boilerplate cut. Every personal detail is made up: the name, the
addresses, the order numbers, the tracking numbers.

`topps_shipped_synthetic.eml` is different: no real Topps order email was available, so its
layout is invented. It shows that a Topps email goes through the shared parser, not that real
Topps emails parse correctly.

When adding a fixture from a real email, replace names, addresses, order numbers, tracking
numbers and every link before committing. This repo is public.
