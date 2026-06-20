# Transaction Isolation Levels

## Read Uncommitted Isolation Level

Read Uncommitted is the lowest isolation level in SQL databases. Transactions can read data that has been modified by other transactions but not yet committed. This level permits dirty reads, where a transaction reads intermediate states that may be rolled back.

Read Uncommitted is rarely used in production systems due to data consistency risks. A transaction might read data that another transaction subsequently rolls back, leading to decisions based on data that never actually existed in the database. Use this level only in scenarios where approximate results are acceptable, such as rough analytical queries on non-critical data or monitoring dashboards that tolerate stale information.

The primary advantage is maximum concurrency and minimal locking overhead. The tradeoff is severe: your application must handle the possibility of reading inconsistent data. Example: Transaction A reads a partially updated account balance that Transaction B later rolls back, causing the reading transaction to operate on incorrect information.

Most database systems support Read Uncommitted, but many applications configure higher levels by default for data integrity reasons.

## Read Committed Isolation Level

Read Committed is the default isolation level in many modern databases including PostgreSQL and Oracle. Transactions can only read data that has been committed by other transactions. Uncommitted or dirty reads are prevented, but non-repeatable reads and phantom reads can still occur.

In Read Committed, each query within a transaction reads the most recently committed version of data at the moment the query executes. If another transaction commits changes between two queries in the same transaction, the second query will see different results. This level balances performance and consistency for most business applications.

Use Read Committed for standard transactional workloads where you need basic consistency guarantees without the performance penalty of higher isolation levels. It prevents lost updates and dirty reads while maintaining reasonable concurrency. The tradeoff is that within a single transaction, you may observe different states of the same data if other transactions commit changes.

Example: Transaction A reads a customer record at the start of a business operation. Before Transaction A completes, Transaction B commits an update to that customer record. When Transaction A queries the same customer again, it sees the committed changes from Transaction B.

## Repeatable Read Isolation Level

Repeatable Read prevents both dirty reads and non-repeatable reads. Once a transaction reads a data item, that same read will return identical results throughout the transaction's lifetime, even if other transactions commit changes to that data.

Repeatable Read typically uses snapshot isolation or shared locks on read data. A transaction receives a consistent view of the database at the point it begins, preventing other transactions from modifying any data it has read. However, phantom reads can still occur—new rows matching a query's WHERE clause may appear if inserted by concurrent transactions.

Use Repeatable Read when you need consistency within a transaction, such as financial reconciliation processes or inventory reservations where you must ensure counts remain stable throughout your operation. The performance cost is moderate compared to Serializable, as it doesn't prevent all concurrent modifications.

Example: A transaction reads all orders for a customer totaling $1000. If it reads those orders again later in the same transaction, it will see the same orders and total, even if other transactions added new orders in between. However, a new query with a different filter might retrieve newly inserted rows.

## Serializable Isolation Level

Serializable is the highest isolation level, providing complete isolation between concurrent transactions. It eliminates dirty reads, non-repeatable reads, and phantom reads. Transactions execute as if they were serialized—the final database state is identical to some sequential execution of all transactions.

Serializable isolation can be implemented through strict locking or optimistic concurrency control with conflict detection. The database either locks all data a transaction might access or validates at commit time that no conflicts occurred with concurrent transactions. This eliminates anomalies but reduces concurrency significantly.

Use Serializable for critical financial transactions, regulatory compliance operations, or systems where data consistency is non-negotiable. The performance cost is substantial—throughput decreases as fewer transactions can execute concurrently. Many systems use Serializable selectively only for the transactions that require it, using lower levels elsewhere.

Example: A transaction transfers money between accounts, reads balances, and verifies constraints. With Serializable isolation, no other transaction can read, insert, or modify any account data this transaction touches until completion, guaranteeing the transfer is consistent with all invariants.

## Dirty Read Prevention

A dirty read occurs when a transaction reads data written by another transaction that hasn't committed yet. If the writing transaction rolls back, the reading transaction has operated on data that never actually committed to the database.

Dirty read prevention is guaranteed at Read Committed isolation level and above. The mechanism typically involves holding write locks until commit or maintaining multiple versions of data. Read Uncommitted explicitly allows dirty reads for maximum performance.

Prevent dirty reads in any production system handling real data. The cost of a transaction acting on rolled-back data can be severe—incorrect calculations, invalid state, or corrupted business logic. Most SQL databases prevent dirty reads by default.

Example: Transaction A updates a product price from $100 to $50 but hasn't committed. Transaction B reads this price as $50 and makes a pricing decision. Transaction A then rolls back, reverting the price to $100. Transaction B's decision was based on data that never persisted.

## Non-Repeatable Read Phenomenon

A non-repeatable read occurs when a transaction reads the same data twice and gets different values. Between the two reads, another transaction committed a modification to that data. The phenomenon violates the expectation that data remains consistent within a single transaction.

Non-repeatable reads are prevented at Repeatable Read isolation level and above. Lower levels (Read Uncommitted and Read Committed) permit this anomaly because they don't maintain consistency of a transaction's view across multiple accesses to the same data.

Prevent non-repeatable reads when you need stable data within a transaction, particularly for operations that make multiple decisions based on the same data item. For example, approval workflows often require that the approval basis remains unchanged throughout processing.

Example: Transaction A reads a customer's credit limit as $10,000, makes a lending decision, then reads it again and finds it's now $5,000 because Transaction B reduced the limit and committed. Transaction A's logic is now based on inconsistent assumptions.

## Phantom Read Phenomenon

A phantom read occurs when a transaction executes a query twice and receives different sets of rows, even though the WHERE clause and query logic are identical. Between executions, another transaction inserted or deleted rows matching the query condition and committed those changes.

Phantom reads can occur at Read Committed and Repeatable Read isolation levels. Serializable isolation eliminates phantom reads by preventing concurrent insertions and deletions in data ranges a transaction reads. Some databases implement range locking to prevent phantoms at lower isolation levels.

Phantom reads pose problems for aggregate operations, pagination, or inventory calculations that depend on complete result sets. If row counts change between iterations, your business logic may make incorrect decisions based on incomplete information.

Example: Transaction A counts orders with status "pending" and finds 50. It processes these orders. Later in the same transaction, it counts pending orders again and finds 55, because Transaction B inserted 5 new pending orders and committed. Transaction A didn't see or process these new orders.

## Write Skew Anomaly

Write skew is a concurrency anomaly where two transactions read overlapping data sets, make independent writes based on assumptions about that data, and commit. The final state violates an invariant that would hold if transactions executed serially.

Write skew differs from a dirty write or lost update—the transactions write to different data items, so traditional locking doesn't prevent the anomaly. It occurs at Repeatable Read and Read Committed isolation levels. Serializable isolation prevents write skew through conflict detection or strict locking.

Write skew can cause subtle data inconsistencies in constraints involving multiple records. Common examples include meeting scheduling (two doctors booking the same room), duplicate detection (two transactions inserting different versions of the same entity), or inventory constraints.

Example: Two doctors each check that at least one doctor is on call (reading the on-call table). Both see another doctor scheduled. Each independently marks themselves as off-call and commits. Result: no doctor is on call, violating the constraint that at least one must be scheduled.

## Multiversion Concurrency Control (MVCC)

MVCC is a concurrency control mechanism where the database maintains multiple versions of each data item. Transactions read from a snapshot of the database at a point in time, while other transactions may be writing new versions. This allows read and write operations to proceed with minimal locking.

MVCC works by timestamping or version-numbering each data modification. When a transaction begins, it's assigned a snapshot version identifier. All reads return data from that snapshot, while concurrent writes create new versions. Old versions are retained until no active transactions reference them.

MVCC enables high concurrency while preventing dirty reads and supporting repeatable read semantics. The cost is increased storage for maintaining multiple versions and overhead for garbage collection of unused versions. PostgreSQL, MySQL InnoDB, and most modern databases use MVCC variants.

Example: Transaction A starts at version 100. Transaction B updates a row and commits, creating version 101. Transaction A's reads still see version 100 of that row, providing consistent snapshots. After Transaction A commits, version 100 becomes eligible for cleanup.

## Snapshot Isolation

Snapshot Isolation is an MVCC-based isolation level where each transaction reads from a consistent snapshot of the database at the moment the transaction begins. Transactions see a stable view unaffected by concurrent modifications, eliminating dirty reads, non-repeatable reads, and phantom reads from the snapshot's perspective.

Snapshot Isolation prevents most common anomalies but permits write skew, as noted above. Two transactions can read the same snapshot, make independent writes to different items based on assumptions about the snapshot state, and both commit. This is weaker than Serializable but stronger than Repeatable Read in some dimensions.

Use Snapshot Isolation for workloads requiring consistency within a transaction without serializable overhead. It's effective for analytical queries, reporting, and batch operations. Be aware of write skew and implement application-level checks for constraints spanning multiple records if necessary.

Example: Transaction A reads the snapshot at version 10. Meanwhile, Transaction B modifies and commits new versions. Transaction A continues reading version 10 state for all queries. When Transaction A commits, it doesn't see any of Transaction B's changes, ensuring internal consistency.

## Lock-Based Isolation Implementation

Lock-based isolation uses explicit locking mechanisms to enforce isolation levels. Transactions acquire read locks (shared locks) or write locks (exclusive locks) on accessed data. Read locks prevent write locks, and write locks prevent all other locks.

Strict locking (holding locks until transaction commit) guarantees Serializable isolation. Weaker locking protocols support lower isolation levels. Lock-based systems must handle deadlock detection and resolution—two transactions waiting for each other's locks require intervention.

Lock-based systems are straightforward to reason about but can suffer from reduced concurrency and potential deadlocks. They work well for short transactions with predictable access patterns. Complex transactions with many dependent decisions can cause long lock holds and contention.

Example: Transaction A acquires an exclusive lock on a row before modifying it. Transaction B attempts to read the same row and blocks waiting for Transaction A to release the lock. Once Transaction A commits and releases the lock, Transaction B proceeds with the most recent committed version.

## Optimistic Concurrency Control

Optimistic concurrency control assumes conflicts are rare and allows transactions to execute without locks. At commit time, the system validates that no conflicts occurred. If a conflict is detected, the transaction is aborted and must be retried.

Optimistic control uses techniques like version numbers, timestamps, or read/write set tracking. The database checks at commit whether any data a transaction read was modified by concurrent transactions. This detection enables validation-based isolation level enforcement without explicit locking.

Optimistic control excels when conflicts are genuinely rare and transaction duration is short. Workloads with frequent conflicts cause high abort rates and repeated retry overhead. The approach is favorable for web applications with many independent users and high read-to-write ratios.

Example: Transaction A reads a product record (version 10). Transaction B modifies and commits the same record (now version 11). Transaction A attempts to commit an update, but validation fails because the record version changed. Transaction A is aborted and must retry.

## Lost Update Prevention

A lost update occurs when two transactions read the same data, each makes independent modifications, and whichever commits last overwrites the other's changes. The first transaction's update is lost, even though both transactions committed successfully.

Lost updates are prevented at Read Committed isolation level and above through locking or conflict detection. At Read Uncommitted, transactions may not see each other's changes properly, potentially losing updates. Optimistic systems detect lost updates during validation.

Prevent lost updates in any system where multiple concurrent modifications are possible. This is especially critical for counters, balances, scores, or any numeric accumulation. Lost updates cause data loss and financial inaccuracy.

Example: Transaction A and B both read a product quantity of 100. Transaction A reduces it to 95 and commits. Transaction B reduces it to 90 and commits. The final quantity is 90, but Transaction A's reduction to 95 was lost. The quantity should be 85 if both reductions applied.

## Conflict Detection and Resolution

Conflict detection identifies when concurrent transactions access overlapping data in conflicting ways (concurrent writes or read-write conflicts). Resolution determines how to proceed—typically aborting one transaction and allowing the other to commit.

Optimistic systems detect conflicts at commit time by comparing read and write sets. Pessimistic systems detect conflicts eagerly through locking. Resolution typically favors the first committer, aborting the later transaction to prevent inconsistency.

Implement conflict detection when using optimistic concurrency control or when implementing custom isolation enforcement. Choose resolution strategies based on your workload: transaction frequency, cost of retries, and whether certain transactions have priority.

Example: Transaction A writes record X, Transaction B writes record X. Both read and modified different records, but they both touched X. The system detects this write-write conflict. Transaction B's commit is rejected, and it must retry, reading Transaction A's committed changes.

## Serialization Graph Testing

Serialization Graph Testing is a conflict-detection technique used to determine if a set of concurrent transactions can be serialized. Each transaction is a node; edges represent conflicts. A cycle in the graph indicates a non-serializable execution.

This technique is theoretical and expensive to implement in practice but illustrates the concept of serializability. Real systems use simpler conflict detection (read-write conflicts) or locking rather than building complete serialization graphs.

Understanding serialization graphs helps reason about isolation and why certain concurrency patterns cause anomalies. It's useful for analyzing specific transaction sequences in testing and debugging.

Example: Transaction A reads X then writes Y. Transaction B reads Y then writes X. The graph has edges A→B (A's write Y conflicts with B's read Y) and B→A (B's write X conflicts with A's read X). The cycle proves this execution isn't serializable.

## Isolation Level Selection Tradeoffs

Choosing an isolation level involves tradeoffs between consistency guarantees and system performance. Higher isolation levels prevent more anomalies but reduce concurrency and increase latency. Lower levels risk data inconsistencies but maximize throughput.

Common tradeoffs: Read Uncommitted maximizes performance but allows dirty reads; Read Committed prevents dirty reads with moderate overhead; Repeatable Read and Serializable provide stronger guarantees but significantly reduce concurrent throughput. Application requirements, data criticality, and workload characteristics should drive the choice.

Most applications don't need uniform isolation levels. Use higher levels for critical operations (financial transactions, inventory updates) and lower levels for non-critical read-heavy operations (analytics, reporting, caching).

Example: An e-commerce system uses Serializable for payment processing and inventory updates, Read Committed for product browsing and search, and Read Uncommitted for rough analytics dashboards showing approximate stock levels.

## Cascading Aborts in Locking Systems

Cascading aborts occur in lock-based isolation when a transaction reading uncommitted changes from another transaction must abort because that other transaction aborted. This creates a cascade: one abort triggers dependent aborts.

Strict locking (holding write locks until commit) prevents cascading aborts by ensuring transactions never read uncommitted data. However, some lock protocols release locks earlier, trading strict consistency for concurrency, which permits cascading aborts.

Avoid cascading aborts in production systems; they complicate recovery and can cause widespread transaction failures from a single fault. Use strict locking for critical operations or redesign to prevent reading uncommitted data.

Example: Transaction A reads uncommitted data from Transaction B. Transaction B aborts, rolling back its changes. Transaction A must now abort too because its read data no longer exists. If other transactions read from Transaction A's results, they cascade further.

## Conservative Locking Strategy

Conservative locking involves acquiring all necessary locks before starting transaction execution. The transaction identifies all data it might access upfront, acquires all locks, then executes without further lock requests.

Conservative locking prevents deadlocks by eliminating circular wait conditions and makes lock acquisition deterministic. The downside is that transactions often acquire more locks than strictly needed if execution paths vary, reducing concurrency.

Use conservative locking for transactions with well-defined, static access patterns. Avoid it for transactions with conditional logic that may access different data based on read results, as you'd need to conservatively lock all possibilities.

Example: A transfer transaction knows it will access two specific accounts. It acquires locks on both accounts before starting, guarantees no deadlock with other transfers, and proceeds without waiting for locks during execution.

## Gap Locking for Phantom Prevention

Gap locking prevents phantom reads by locking ranges of values in indexes, not just individual rows. When a transaction reads rows matching a condition, gap locks prevent other transactions from inserting new rows into that range.

Gap locking is more complex to implement than row locking but necessary for preventing phantoms at lower isolation levels. It requires index infrastructure and careful management to avoid locking excessively large ranges.

Gap locking is primarily used in databases implementing Serializable isolation through locking. MVCC-based systems typically prevent phantoms differently, through snapshot isolation.

Example: A transaction queries "orders with status='pending' and date>'2024-01-01'". The database acquires gap
