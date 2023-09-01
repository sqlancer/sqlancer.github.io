---
title: 'GSoC 2023: Final Report of test-case reduction'
date: 2023-8-30
author: Yutan Yang
categories:
  - blog
tags:
  - gsoc
---

## Overview

SQLancer generates a large number of statements, but not all of them are relevant to the bug. To automatically reduce the test cases, [Yutan Yang](https://github.com/ColinYoungTaro) implemented two reducers in SQLancer, the statement reducer and the AST-based reducer.


## Statement Reducer

The statement reducer utilizes the delta-debugging technique, which is designed for efficient test case reduction, to reduce the statements. The set of bug-inducing test cases is divided into subsets. Each subset is then individually removed. If the bug is still triggered without a specific subset, this subset is considered to be irrelevant and thus can be eliminated. Conversely, if removing any subset does not reproduce the bug, the reducer will divide test cases into more subsets and repeat the process. This iterative process continues until the set cannot be further divided into more subsets. 

For more details, you can refer to the paper: [Simplifying and Isolating Failure-Inducing Input](https://www.cs.purdue.edu/homes/xyzhang/fall07/Papers/delta-debugging.pdf) or [this video tutorial](https://youtu.be/lGe2-y1xibY). 

Delta-debugging offers the advantage of potentially removing a significant number of statements in one turn, making it particularly efficient when the bug-inducing statements are sparsely distributed.

Using the statement reducer, SQLancer reduces the set of statements to a minimal subset that reproduces the bug. 

## AST-Based Reducer

The AST-based reducer can shorten a statement by applying AST level transformations. They are mostly implemented using [JSQLParser](https://github.com/JSQLParser/JSqlParser), a RDBMS agnostic SQL statement parser that can translate SQL statements into a traversable hierarchy of Java classes. JSQLParser provides support for the SQL standard as well as major SQL dialects. The AST-based reducer works for any SQL dialects that can be parsed by this tool.
q

Currently, the AST-based transformations include:

+ Remove union selects.  e.g., 
  `SELECT 1 UNION SELECT 2` -> `SELECT 1` 
+ Remove irrelevant clauses.  e.g., 
  `SELECT * FROM t OFFSET 20 LIMIT 5` -> `SELECT * FROM t`
+ Remove list elements.  e.g.,
   `SELECT a, b, c FROM t` -> `SELECT a FROM t`
+ Remove rows of an insert statement.  e.g., 
  `INSERT INTO t VALUES (1, 2), (3, 4)` -> `INSERT INTO t VALUES (1, 2)`
+ Replace complicated expressions with their sub expressions.  e.g., 
  `(a+b)+c` -> `c`
+ Simplify constant values.  e.g., 
  `3.27842156` -> `3.278`

The AST-based reducer is designed with extensibility. It walks through the list of transformations and applies them to statements until a fixed point is reached. Adding new transformations is straightforward. Simply create a new transformation and include it in the list. This flexibility allows for easy customization and expansion of the reducer's functionality. Additionally, it is also possible to support transformations that do not depend on JSQLParser.



## Framework for reducers Testing

Yutan designed a virtual database engine to facilitate the testing of reducers. Instead of executing the statements, this engine records them for analysis. One of its notable features is the ability to customize the conditions that trigger a bug. For instance, the testing framework allows specifying an interestingness check that tests whether certain words are part of the reduced SQL test case.

This approach provides a testing environment and allows for thorough evaluation of the reducers' performance. It offers convenience and flexibility in refining and polishing the functionality of the reducers without impacting real databases.

## Reduction logs

If test-case reduction is enabled, each time the reducer performs a reduction step successfully,it prints the reduced statements to the log file, overwriting the previous ones.

The log files will be stored in the following format: `logs/<DBMS>/reduce/<database>-reduce.log`. For instance, if the tested DBMS is SQLite3 and the current database is named database0, the log file will be located at `logs/sqlite3/reduce/database0-reduce.log`.

## Usage
Test-case reduction is disabled by default. The statement reducer can be enabled by passing `--use-reducer` when starting SQLancer. 

If you wish to further shorten each statements, you need to additionally pass the `--reduce-ast` parameter so that the AST-based reduction is applied. 

Note: if `--reduce-ast` is set, `--use-reducer` option must be enabled first.

There are also options to define timeout seconds and max steps of reduction for both statement reducer and AST-based reducer.

For more details, you can refer to this [doc](https://github.com/sqlancer/sqlancer/blob/main/docs/testCaseReduction.md).