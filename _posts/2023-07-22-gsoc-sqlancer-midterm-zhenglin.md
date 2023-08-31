---
title: 'GSoC 2023: Midterm Report on Support of StoneDB'
date: 2023-06-04
author: Zhenglin Li
categories:
  - blog
tags:
  - gsoc
---

# Support of StoneDB Midterm Report

SQLancer is an open-source tool for testing the correctness of SQL database systems and supports close to 20 database systems. The goal of this project is to add support for StoneDB to SQLancer and test StoneDB to find potential bugs.

StoneDB is an open-source hybrid transaction/analytical processing (HTAP) database designed and developed by StoneAtom based on the MySQL kernel. It provides features such as high efficiency and real-time analytics, offering a one-stop solution to process online transaction processing (OLTP), online analytical processing (OLAP), and HTAP workloads.

## Things Achieved

See the detailed description of supported syntax here: [Functions Supported by SQLancer for StoneDB](https://docs.google.com/document/d/12OpiDYs_Civor-saKZFmZPZd5ElVJAc9RDpDIwikh9Y/edit?usp=sharing)

See the detailed description of bugs found here: [Bugs Found in StoneDB by SQLancer](https://docs.google.com/document/d/1N-oUGVATV0l6tG87uOtPNmfLS7g_fuo7HIckFobD-Yo/edit?usp=sharing)

## Encountered Challenges and Solutions

This section will cover challenges encountered and the way I addressed them.

### Choose which StoneDB version to support

During the **initial stages** of our project, our plan was to support the **latest version** of StoneDB, which is **version 8.0**.

However, we encountered several challenges that hindered our ability to work with the 8.0 version. Firstly, there was no available Docker image to facilitate the easy execution of the 8.0 version. Additionally, building StoneDB from source code presented difficulties related to missing packages and incompatible versions within the environment.

Considering these obstacles, we made the decision to **prioritize the support for StoneDB version 5.7 initially**. And by the midterm evaluation, we have successfully implemented the basic support of 5.7 version.

Looking ahead, as we **plan to extend our support to the 8.0 version**, we have identified **two potential approaches**.

1. If the **differences** between the 5.7 and 8.0 versions are **minimal**, we can enhance the existing implementation by introducing a **parameter flag** that handles the version-specific variations. This approach allows us to maintain a single unified implementation that can accommodate both versions.
2. If the **differences** between the versions are **large**, we need to **implement separate code paths** for each version, treating them as distinct entities.

### Which generator to use: typed or untyped

Looking at the three files:

- [sqlancer/common/gen/ExpressionGenerator.java](https://github.com/sqlancer/sqlancer/blob/main/src/sqlancer/common/gen/ExpressionGenerator.java)
- [sqlancer/common/gen/TypedExpressionGenerator.java](https://github.com/sqlancer/sqlancer/blob/main/src/sqlancer/common/gen/TypedExpressionGenerator.java)
- [sqlancer/common/gen/UntypedExpressionGenerator.java](https://github.com/sqlancer/sqlancer/blob/main/src/sqlancer/common/gen/UntypedExpressionGenerator.java)

The `ExpressionGenerator` is an interface, and `TypedExpressionGenerator` and `UntypedExpressionGenerator` are abstract classes that implement the `ExpressionGenerator` interface.

When implementing the concrete generator, we have to decide which generator to inherit from, the `TypedExpressionGenerator` or the `UntypedExpressionGenerator`.

A **typed generator** is a generator which is **associated with a specific data type or set of data types**. One characteristic is that it allows for generating values of the type that is expected in a specific context. On the other hand, an **untyped generator** **does not** have a specific data type associated with it. It can yield values of any type.

The choice between using a typed or untyped generator **depends on the requirements of the DBMS under test**. Typed generators provide the advantage of generating expected types when this is important. Untyped generators, on the other hand, offer more **flexibility** and can be useful in scenarios where the type of yielded values may vary or is not known in advance.

Ultimately, I decided to use the untyped generator because it can yield values of any type, allowing us to test the support of unexpected types of a DBMS.

### Read and understand the code of the implementation of other DBMS in SQLancer

During the initial phase of our project, I delved into the codebase of various DBMS implementations within SQLancer to gain a comprehensive understanding.

The first challenge I encountered is due to the limited comments within the code. Consequently, I had to rely on studying the source code to comprehend the design and functionality of classes, methods, and functions. The good news is though the implementation details may differ from one database to another, there are still some common rules to follow. So, I read the code of many other DBMS and identified the common parts and started my implementation.

## Interaction With StoneDB Community

This section will cover our interaction with the StoneDB community.

We actively engaged with the StoneDB community to seek assistance, share our findings, and collaborate on improving the system. Our interaction primarily took place through the **Slack channel** and **GitHub platform**.

In addition to Slack, we also utilized the issue and discussion sections on GitHub to communicate with the StoneDB community. All questions or comments got replies. For example,

- They answered our question here: [stonedb-8.0-v1.0.1-beta](https://github.com/orgs/stoneatom/discussions/1849#discussioncomment-6138954)
- They fixed [the docker size bug](https://github.com/stoneatom/stonedb/issues/1923) we found previously
- They are trying to fix the [bad dpn index when deleting rows bug](https://github.com/stoneatom/stonedb/issues/1933)
- They are trying to fix the [crash: StoneDB crash when executing the command (SELECT, HAING, GROUP BY, IS NULL)](https://github.com/stoneatom/stonedb/issues/1941)
- They are trying to fix the [bug: query result not correct](https://github.com/stoneatom/stonedb/issues/1942)

## Future Work

This section will cover opportunities for potential future changes.

1. Test StoneDB, add more expected errors, and report potential bugs.

2. Support more statement categories and syntax. For example, referring to the docs of MySQL, there are some optional arguments that we may not currently support. We can support them in the future.

3. Another example is that, we can support more functions, refer to: [https://stonedb.io/docs/SQL-reference/functions/advanced-functions](https://stonedb.io/docs/SQL-reference/functions/advanced-functions)

4. Refine the code implementation. For example, we can discuss if we can reuse some code that is common to all DBMS implementations.
