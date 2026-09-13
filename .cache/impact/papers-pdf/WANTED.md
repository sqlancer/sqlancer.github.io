# Papers to download

13 papers whose publisher will not serve a PDF to an automated client. Downloading one is entirely optional: a missing paper costs coverage for that paper, never correctness.

Save each file into this directory under the **Save as** name — that is how the pipeline finds it — then re-run:

```sh
python3 -m tools.impact.run collect --only papers --full
```

Claims drawn from a supplied PDF quote it verbatim, exactly as they would from a preprint, so they stay checkable by anyone holding the same paper.

Papers on arXiv, in PVLDB, or at a USENIX venue are not listed: the pipeline reads those itself.

IEEE links go through the NUS library proxy, which is what makes them resolve; ACM and Springer links point straight at the PDF.

| # | Paper | Download | Known so far | Save as |
| ---: | --- | --- | --- | --- |
| 1 | [A Formal Framework for Typing and Cast Semantics in SQL Engines](https://doi.org/10.1145/3834861) (2026) | [ACM PDF](https://libproxy1.nus.edu.sg/login?url=https://dl.acm.org/doi/pdf/10.1145/3834861) | cites only (3 sentences hint at more) | `10.1145_3834861.pdf` |
| 2 | [MTKeras: An Automated Metamorphic Testing Platform](https://doi.org/10.1142/s021819402150039x) (2021) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.1142/s021819402150039x) | cites only (1 sentences hint at more) | `10.1142_s021819402150039x.pdf` |
| 3 | [A Framework for Systematic Analysis and Automated Detection of Dark Patterns on E-Commerce Websites](https://doi.org/10.4018/979-8-2600-0747-1.ch001) (2026) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.4018/979-8-2600-0747-1.ch001) | cites only | `10.4018_979-8-2600-0747-1.ch001.pdf` |
| 4 | [Evaluating ERD Models and RAID-Based Storage for Query Performance Optimization in Relational Databases](https://doi.org/10.35970/jinita.v7i1.2707) (2025) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.35970/jinita.v7i1.2707) | cites only | `10.35970_jinita.v7i1.2707.pdf` |
| 5 | [Language-Based Testing for Knowledge Graphs](https://www.semanticscholar.org/paper/5bef8601da9663add6a85713414f8c945cf97355) (2025) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://www.semanticscholar.org/paper/5bef8601da9663add6a85713414f8c945cf97355) | cites only | `s2_5bef8601da9663add6a85713414f8c945cf97355.pdf` |
| 6 | [Real-World Scalability of PostgreSQL: Practical Techniques Use Case Study](https://doi.org/10.1109/icbds67396.2025.11377486) (2025) | [IEEE PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.1109/icbds67396.2025.11377486) | cites only | `10.1109_icbds67396.2025.11377486.pdf` |
| 7 | [Siloso: Finding Logic Bugs in RDBMS via Dialect-Adaptable Reference Engine Construction](https://doi.org/10.1145/3758316.3763253) (2025) | [ACM PDF](https://libproxy1.nus.edu.sg/login?url=https://dl.acm.org/doi/pdf/10.1145/3758316.3763253) | cites only | `10.1145_3758316.3763253.pdf` |
| 8 | [Leopard: A General Test Suite for Isolation Level Verification](https://www.semanticscholar.org/paper/07f4bfcce4dd87d401b70baaa9aa6f9ef5d0ed85) (2024) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://www.semanticscholar.org/paper/07f4bfcce4dd87d401b70baaa9aa6f9ef5d0ed85) | cites only | `s2_07f4bfcce4dd87d401b70baaa9aa6f9ef5d0ed85.pdf` |
| 9 | [Emerging Aspects of Software Fault Localization](https://doi.org/10.1002/9781119880929.ch13) (2023) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.1002/9781119880929.ch13) | cites only | `10.1002_9781119880929.ch13.pdf` |
| 10 | [Siklus Hidup Pengembangan Sistem Basis Data Pada Sistem Informasi Buku Tamu di Badan Pusat Statistik Kabupaten Kediri Menggunakan MySQL](https://doi.org/10.32672/jnkti.v6i1.5830) (2023) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.32672/jnkti.v6i1.5830) | cites only | `10.32672_jnkti.v6i1.5830.pdf` |
| 11 | [Database System Development Life Cycle (DSDLC) on System Libraries for Data Manipulation Language (DML) Using SQL Server 2008](https://doi.org/10.35335/jurnalmantik.vol5.2021.1448.pp1065-1071) (2021) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.35335/jurnalmantik.vol5.2021.1448.pp1065-1071) | cites only | `10.35335_jurnalmantik.vol5.2021.1448.pp1065-1071.pdf` |
| 12 | [Differential Monitoring - Technical Report ⋆](https://www.semanticscholar.org/paper/b6c64dbe0130ef1bf6af12266b958a5d5396101f) (2021) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://www.semanticscholar.org/paper/b6c64dbe0130ef1bf6af12266b958a5d5396101f) | cites only | `s2_b6c64dbe0130ef1bf6af12266b958a5d5396101f.pdf` |
| 13 | [Verifying Serializability Protocols With Version Order Recovery](https://doi.org/10.3929/ethz-b-000507577) (2021) | [other PDF](https://libproxy1.nus.edu.sg/login?url=https://doi.org/10.3929/ethz-b-000507577) | cites only | `10.3929_ethz-b-000507577.pdf` |
