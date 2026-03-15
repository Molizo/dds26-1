Distributed Data Systems
Transaction Processing & Concurrency Control

Asterios Katsifodimos
George Christodoulou

1

Today

• Transaction Processing
• In data systems many things can go wrong

• Failures, crashes, network interruptions etc.
• System should stay reliable
• Different levels of guarantees

2

What is a transaction?

•

•

A transaction is a unit of work, possibly containing multiple data accesses and updates, that must commit or
abort as a single unit, and follows the ACID principles
e.g, transfer $100 from account A to account B

A = $400
B = $200

read(A, t);
t := t – 100;
write(t, A);
read(B, t);
t := t + 100;
write(t, B)

A = $300
B = $300

■ Transactions introduce two problems:

□ Recovery: what if the system fails in the middle of execution?
□ Concurrency: what happens when two transactions try to access the

same object?

3

Transaction Workflow Examples

• Automatic teller machines (ATM)

– User Interaction

Insert your card and input PIN code

•
• Select amount
• Take card and cash

– Basic business workflow

• Authenticate user
• Ask for requested amount
• Query for available balance (read operation): if balance is too low eject card and

abort…

• Else deduct amount from balance (write operation)
• Return card and dispense cash

4

Transaction Processing

• Depending on the application, certain database

interactions belong to the same business workflow
– Queries (read operations)
– Updates, deletes, inserts (write operations)

• Database operations are often intertwined
• Typical workflow examples
– Money transfers in banking
– Travel booking

5

Problems

• Still, while processing workflows problems can occur

– Even if we assume that individual workflows are always sensible and

correct
• Examples

– What if the ATM catches fire after withdrawing your money, but before

dispensing it..?!

– What if you found the perfect flight and hotel, but when trying to buy the

ticket, somebody else has already bought the last ticket in the
meantime..?!

6

Transactions

• For avoiding these problems we need the concept of

transactions
– A transaction is a finite set of operations (workflow, program)
that has to be performed in a certain order, while ensuring
certain properties

• The properties are concerned with

– Integrity: transactions can always be executed safely, especially in

concurrent manner, while ensuring data integrity

– Fail-safety: transactions are immune to system failures

7

Transactions

• Application programs should not be bothered with data

integrity and protection issues
– DBMS holds responsibility for ensuring the well-being of data
– Transactions are an interface contract of a transaction-enabled server

• Start: Starts a transaction, followed by a finite sequence

of operations of a workflow or program

• Commit: Executes all operations since transaction begin and ends the

transaction

• Rollback: Cancels the current transaction and reverts all effects of the

transaction operations

8

Transactions

• Our simple initial view on transactions in this lecture
– … many DBMS systems stick to these simple assumptions
– A single central database server

• No agreement and communication between multiple servers

– Data stored non-redundantly
• Only one copy of each data item

– Multiple concurrent transactions which need to be interleaved

9

The ACID Principle

• Database transactions show certain properties, also

known as the ACID principle
– Atomicity
– Consistency
– Isolation
– Durability

– Every system handling non-ACID transactions has to take special

precautions

10

The ACID Principle

• Atomicity

– Any transaction is either executed completely, or not at all

• Complete transaction is to be treated as an uninterruptable single operation
• That means, all effects of a transaction have to be materialized in the database

once it has been executed

• The effects will only become visible to other users or transactions if and when the

transaction is committed

11

The ACID Principle

• Consistency

– Transactions lead from one consistent state of the data instance

to another
• Especially, data constraints are always respected
• Transactions that cannot reach a consistent state

(any more) have to be aborted

– Consistent state of the data involves the application

12

The ACID Principle

• Isolation

– Transactions are isolated from others, i.e. even in a concurrent

scenario transactions do not interfere with each other
• The effect of a transaction always has to be the same as if it had been

executed on its own

• Moreover, each transaction will read only consistent data

from the data store

13

The ACID Principle

• Durability

– As soon as the transaction is completed (committed), all

performed data changes are guaranteed to survive system
failures
• Either the data is permanently written on the disk, or specific means for

recovery have been taken

14

Isolation Levels & Concurrency issues

• Concurrency issues

– Hard to test

• These bugs occur rarely and are difficult to reproduce

• Transaction Isolation guarantees

–  (Usually) a trade-off between correctness and performance

• Based on Read, Write dependencies, timing

15

Concurrency issues (how to preserve isolation)

• Concurrency issues

– Dirty reads
– Dirty writes
– Non repeatable reads
– Phantom reads
– Write Skew

16

Concurrency issues

• Dirty reads

• Reading uncommitted data

Transaction A

Write(X = 20)

Transaction B

Read(X)

X = 20

X = 20
(Uncommitted)

X = 10

Abort

X = 10

17

Concurrency issues

• Non-repeatable Reads

• Re-reading the same row gives different results

Transaction A

Transaction B

Read(X)

Read(X)

X = 10

Write (X = 50)

Commit

X = 50

X = 10

X = 50
(Uncommitted)

X = 50

18

Concurrency issues

• Phantom Reads

• Query returns different set of rows when re-executed

Transaction A

Transaction B

Read(Values > 5)

Read(Values > 5)

2 Values
returned

Insert (Z = 50)

Commit

3 Values
returned

X = 10
Y = 20

X = 10
Y = 20
Z = 50

19

Concurrency issues

• Dirty writes

• Two transactions write uncommitted changes over each other

Write(X = 40)

Transaction A

Transaction B

Read(X)

X = 40

If (X > 30)
Write(Y = 50)

Commit

Abort

X = 40
(Uncommitted)

X = 40
Y = 50
(Uncommitted)

X = 10
Y = 10

X = 40
Y = 50

20

Concurrency issues

• Write Skew

• Two transactions read overlapping data and then write disjoint but logically

conflicting data

Transaction A

Transaction B

Read(X)

X = 10

Read(Y)

Y = 10

Write(Y = 20)

Commit

Commit

Write(X = 20)

X = 10
Y = 10

X = 10
Y = 20
Version A

X = 20
Y = 10
Version B

21

Concurrency issues

• Lost Updates

• Two transactions read the same row, both update it based on the read

value, and the first write is overwritten by the second

Transaction A

Transaction B

Read(X)

X = 10

Read(X)

X = 10

Write(X++)

Commit

Commit

Write(X++)

X = 10

X = 11

X = 11 Should be 12

22

Isolation Levels

• Read uncommitted

• Transactions can see uncommitted changes

Does not suffer from
Suffers From

Dirty reads

Dirty writes

Non-repeatable reads

Phantom reads

Write skew

• Read committed

• Transactions see changes made by other committed transactions
• Does this isolation level give us all the guarantees we need?
• No, it does not avoid read skew (non-repeatable read)

Dirty reads

Dirty writes

Non-repeatable reads

Phantom reads

Write skew

23

Isolation Levels

• Snapshot Isolation

Does not suffer from
Suffers From

– “Serializable” in Postgres, “Repeatable read” in MySQL
– Consistent snapshot of the database

• Used for consistent reads

– Multiple versions of objects exist

• Multiversion concurrency control (MVCC)
• “Lost updates” can occur

Dirty reads

Dirty writes

Non-repeatable reads

Phantom reads

Write skew

24

Isolation Levels

• Serializability

– Strongest isolation level

Does not suffer from
Suffers From

• Same as if all transactions were executed serially

– Performance trade-off

• Optimistic Concurrency Control (OCC), 2PC +2PL
• Prevents conflicts by blocking transactions from conflicting access

Dirty reads

Dirty writes

Non-repeatable reads

Phantom reads

Write skew

25

Isolation Levels

• Serializable Snapshot Isolation

Does not suffer from
Suffers From

– Every transaction executes without issues
– Implementation technique to achieve better concurrency
– Optimistic approach

• Transactions execute, checked at commit
• Allows for more concurrency
• “write skew” can happen, but is detected and aborted

Dirty reads

Dirty writes

Non-repeatable reads

Phantom reads

Write skew
(Detects and aborts them)

26

Guarantees

• Linearizability

– Aka atomic consistency, strong consistency, immediate

consistency or external consistency
• The idea: If you ask two different replicas the same question, you should

not get two different answers

27

Guarantees

• Example

Client A

*Beats boss in game for the first time*,
Insert achievement into leaderboard

primary

secondary

secondary

Client B

Client C

ok

insert..

Select * from
leaderboard

insert..

Finally, someone
with a new
highscore

Really?
Leaderboard is
still the same

28

Guarantees

• Linearizability

– But how is serializability different?

• Consistency guarantee on replicated data
•

Linearizability is a recency guarantee on reads and writes of a (virtually) single object

– Serializability

• An isolation property of transactions
• Guarantees that transactions behave the same as if they had executed in SOME* serial

order

–  Strict Serializability

• The combination of both, gives us strict serializability

 (*ok to be different than the actual order)

29

Guarantees

• Serializable Snapshot isolation

– And then what is SSI and how is it different?

• SSI is not linearizable
• SSI does not enforce real-time ordering of commits
•

In other words: a transaction that started before another is not enforced to not
appear after it in the “serial” order.

– One consistent snapshot

• The point is to not include writes that are more recent than the snapshot used

for reads

• Consistent reads
• No contention between readers & writers

30

Guarantees

• Causality

– Partial order

• Two operations can be causally related (“happens before”)
• Concurrent operations are incomparable

– Total order

• Linearizability offers total order
• System behaves as if a single copy of data exists
• All operations are atomic
• Any two operations can be compared and ordered

31

Page Model

• Core abstraction where the database is divided into fixed-

size pages

• Transactions interact with the database with read or

write requests on specific pages

• Concurrency control is implemented on page level
• Simplifies disk I/O complexities

32

Page (or block or buffer) Model

• Definition: A transaction is a totally-ordered finite

sequence of actions of the form r(x) or w(x) where x  is
some record from the database instance
– r(x) denotes a read operation
– w(x) denotes a write operation

• Example of transaction:

– T  := r(x) r(y) r(z) w(u) w(x)

33

Schedules

•  A schedule is a sequence of operations that
• Contains all the operations of the involved transactions
• Respects the (partial) order of operations within each single transaction
– Serial, when each transaction is fully executed before the next

starts

– Always consistent, low throughput

– Non Serial, Operations from different transactions are interleaved

in time, allowing concurrent transactions

– Higher throughput, consistency needs to be checked

34

Schedules

• Example (without starts and commits)

r(x) r(y) w(u) w(x)

r(p) r(q) w(p)

– T1  :=
T2  :=
T3  :=

r(z) w(z)
• Serial Schedule

– S :=

r(p) r(q) w(p)
r(x) r(y) w(u) w(x)
• Non-serial/interwined schedule

r(z) w(z)

– S :=

r(x) r(y)

r(p)

r(z) w(u)

r(q) w(x) w(z) w(p)

35

Consistency by Scheduling

• Restricting the system to serial schedules will remove all

problems of concurrency control
–  But… what about performance?

• Two schedules are called equivalent, if
– They comprise the same set of operations
– Every transaction in both schedules reads  and writes the same

values from/to a given record

36

Serializable Schedules

• A complete schedule is called serializable, if it is

equivalent to any serial schedule of the respective set of
transactions
– has the same effects as if executed transactions serially, but it

still allows for concurrent execution

37

Schedule Equivalence

• Serial schedules are a proper subset of serializable

schedules

all

serial

serializable

38

Consistency by Careful Scheduling

• Restricting the system to serializable schedules will lead

to consistent concurrency control

• Can we test a schedule for serializability?

– Yes, we can test equivalence to any possible serial schedule

• High Complexity (NP-Complete)
–  Proof builds on graph theory
– Testing schedules for real serializability is not practical in reality

• How do we achieve serializability in the real world?

39

Conflict
Serializability

NP-Complete

Polynomial Time

all

conflict serializable

serial

serializable

40

Conflict Serializability – conflicting operations

• Two operations in a schedule are conflicting, if

– They belong to different transactions
– They access the same database record (or page)
– And at least one of them writes data

•

i.e. dirty reads, phantoms, etc. possible

• All pairs of conflicting operations are called the conflict relation of a

schedule
– Aborted transactions can be removed from a schedule’s conflict relation

• Why do we need this?

41

Conflict Serializability – conflict equivalence

• Two schedules are called conflict equivalent, if

– They contain the same operations
– And have the same conflict relations (i.e., in the same order)

• Conflict relations: Tuples are two (ordered) operations which belong to different

transactions, access the same item, and at least one is a write.

• Example for conflict equivalent schedules
r(z) w(x) w(y)

r(x) r(y) w(z) w(y)

r(x) r(y) w(y)

w(z) w(x)

r(z) w(y)

– S :=
– S’ :=

– Conflict relations:

r(y)

w(y)

w(y)

w(y)

42

Conflict Serializability

• Conflict serializable schedules are a proper subset of all serializable

schedules

• Serial schedules are a proper subset of conflict serializable schedules

all

conflict serializable

serial

serializable

43

Conflict Graphs

• A conflict graph of a schedule S consists of

– All committed transactions of S as nodes
– Between any two nodes t and t‘ there is a directed edge, if the

pair of operations (p, q) is in the conflict set of S (with p ∈ t and q
∈ t‘)

• In conflict graphs…

– If there is an edge between two transactions there is at least one

conflict between them

– The direction of each edge respects the ordering of the

conflicting steps

44

Conflict Graphs

• Example for a conflict serializable schedule

– S :=
– Ss :=

r(x)

r(x) w(x)

r(x) w(x) w(y) w(y)

r(x) w(y)

r(x) w(x) w(y)

r(x) w(x)

– Conflict graph

t1

r(x) w(x)
w(x) w(x)

w(x)

r(x)

r(x) w(x)
w(y) w(y)

t3

t2

r(x) w(x)

45

Conflict Graphs

• Now, how does the graph of some non-conflict

serializable schedule look like?
– Intuition: if an operation conflicts with some other operation, the

operation should be performed after the other in a serial
schedule

r(x)

r(x) w(x)

– S :=
– As we can see the conflict
graph contains a cycle!

w(x)

r(x)

w(x)

t1

t2

r(x)

w(x)

w(x)

w(x)

46

Putting theory together

• Serial schedules: the holy grail of transactions

– They ensure correct execution
– Avoid concurrency issues or anomalies
– But they are slow

• Serializable: a schedule that has the same
effects on a database as a serial schedule.

all

conflict serializable

serial

serializable

• Conflict-serializable: a schedule that has exactly the

same conflicting operations (in the same order) as a serial
schedule.

47

Guarantees

• How to build systems with guarantees

– Consensus, quorums
– Lamport clocks
– CAP
– 2PC, atomic commit
– 2PL

48

Recap

• Transactions & ACIDity
• Transaction Anomalies (consistency, phantoms, etc.)
• Serializability theory
• How to build systems to support transactions

49

Recap

• Problems with transactions occur when different

transactions mutate the same data items

• Multiple isolation levels exist
• Multiple ways to reason about isolation
• Performance-correctness trade-off applies

• Concurrency vs correctness

50

Useful Reads

• Designing Data Intensive Apps, Transactions, Chapter 7
•

Fekete et al., “Making Snapshot Isolation Serializable,” ACM Transactions on
Database Systems

• Bernstein et al., Concurrency Control and Recovery in Database Systems.,

available online at research.microsoft.com.

• Database System Concepts, Transactions (slides available)

51

Two-phase Locking

i.e., one way to guarantee serializability, without the need to
check schedules for conflict-equivalence

52

How do we construct a serializable schedule?

• Option #1

– mix all transaction operations randomly
– then check for conflict-equivalence to a serial schedule
– should we do that?

• Option #2

– Build a scheduler:

• receive a batch of transactions
• Order operations in a manner that creates a serializable schedule

53

Building a Scheduler

• Big question: How do you properly schedule serializable

transactions in a DBMS?
– Design Concurrency Control Protocols

• Needs to consider incomplete transactions, or rollbacks

– Multiple ways to go about this:

• Pessimistic Protocols

– 2-phase locking and derivatives
– …

• Optimistic

– Timestamp Ordering Protocols
– Multi-Version protocols
– …

54

Locking Schedulers

• Traditional & practical schedulers are locking schedulers
– Locks can be set on and removed from data items on behalf of

transactions
• Locking should be an atomic operation
• The lock granularity defines what is actually locked (data records, pages,

etc.)

• Once a lock has been set on behalf of a transaction, the respective item is

not available to other transactions

55

Lock Conflicts

• If a lock is requested by some transaction, the scheduler
checks whether it has already been issued to another
transaction
– If not, the transaction can acquire the lock

• Else a lock conflict arises, the requesting transaction is blocked by the

scheduler, and has to wait

– Eventually, the running transaction will release its lock and the
scheduler can check whether the waiting transaction can be
resumed

56

Lock Conversion

• For conflict-free data access there are two types of locks

– Read locks can be shared by several transactions
– Write locks are exclusive locks

• Compatibility of locks

– Remember: serializability conflicts always

include at least one write operation
– Interesting fact: snapshot isolation does

not care about read-write conflicts
(allowing dirty writes and write skew)

Lock requested

read
lock

write
lock

Lock
held

read
lock

write
lock

yes

no

no

no

57

Two-Phase Locking Protocol

• How can we actually generate a conflict-serializable schedule?
• Prominent technique: Two-Phase Locking (2PL)

– Locks are granted in a growing phase
– Locks are released in a shrinking phase

•

This means, that for each transaction all necessary
locks are acquired before the first lock is released.

#locks

lock point

lock
phase

unlock
phase

start TA

commit point

58

Two-Phase Locking Protocol

• Two-phase locking protocols are a simple way to generate

only serializable schedules
– S :=

lock(x) r(x) lock(y) r(y)

lock(p) r(p) w(p)

unlock(p)

w(x) unlock(x) unlock(y)

– Each legal 2PL schedule is serializable

• Actually 2PL schedules are a proper subset of conflict

serializable schedules
– S2 :=
r(x) w(x)

r(y)
• S2 is conflict serializable,  but not in 2PL

r(x) w(y)

– Lock on x must be released before acquiring lock on y

59

Two-Phase Locking Protocol

• 2-Phase-Locking schedules are a proper subset of

Conflict serializable schedules

all

conflict serializable

2PL

serial

serializable

60

Two-Phase Locking Protocol

• 2-Phase-Locking strong points

– Correctness

• Ensures serializability

– Straight-forward implementation

• simple to build into a transaction manager

– Intuitive concept

• 2 phases, one for acquiring locks, one for releasing

61

Two-Phase Locking Protocol

• 2-Phase-Locking weak points

– Deadlock: conflicts during growing phase

• How would you solve a deadlock?

– Cascading rollbacks

• T1 releases a lock which then is immediately acquired by T2
• T1 fails. T2 must be rolled back (cascading rollback) because it has read a

dirty value from T1
– Reduced Concurrency

• Transactions hold locks for a long time

62

Distributed Database
Concepts

63

Shopping cart example

Online
Store

Stock

Order

Payment

64

Shopping cart example (1)

transaction

Online
Store

y
a
P

Success or failure

Stock

Order

Payment

65

Shopping cart example (2)

response (success or failure)

Online
Store

Abort or commit

Stock

Order

Payment

66

Shopping cart example (3)

This ensures atomicity

response (success or failure)

Online
Store

Abort or commit

Stock

Order

Payment

67

Shopping cart example (3)

How about isolation/consistency?

response (success or failure)

Online
Store

Abort or commit

Stock

Order

Payment

68

SAGAs

“Lightweight” distributed transactions

Hector Garcia-Molina and Kenneth Salem. 1987. Sagas. (SIGMOD '87).

69

Sagas

transaction

Online
Store

Local transaction

Stock

Order

Payment

70

Sagas

transaction

Online
Store

Not enough credits

Stock

Order

Payment

71

Sagas

transaction

Online
Store

Not enough credits

Stock

Order

Payment

72

Sagas

EXTRA BUSINESS
LOGIC

transaction

Online
Store

EXTRA BUSINESS
LOGIC

Not enough credits

Stock

Order

Payment

73

Sagas

transaction

Online
Store

Not enough credits

Stock

Order

Payment

74

Sagas

transaction

Online
Store

Not enough credits

Stock

Order

Payment

75

Two-Phase Commit

76

Two Phase Commit (2PC) - Overview

• Widely used family of protocols in distributed databases.

•

•

•

Two Roles:
– One coordinator.
– n workers (Cohorts).

Assumptions:
– Write-ahead log at each node.
– No node crashes forever.
– Any two nodes can communicate.

Transactions are processed in two phases:
– All nodes try to apply the transaction (writing it to the log).
– The transaction is only committed if all nodes succeeded.
– ➔ All or nothing: Either all nodes commit, or none does.

77

Two Phase Commit (2PC) – Commit Request Phase

Worker

Worker

1. System receives user

request.

Query

User

Coordinator

Worker

Worker

78

Two Phase Commit (2PC) – Commit Request Phase

Worker

Worker

prepare

User

Coordinator

prepare

1. System receives user

request.

2. Coordinator sends

„Prepare
Commit“ message to
all workers.

Worker

Worker

79

Two Phase Commit (2PC) – Commit Request Phase

Log

Log

Worker

Worker

ready / failure

User

Coordinator

ready / failure

Log

Log

Worker

Worker

•

1. System receives user

request.

2. Coordinator sends

„Prepare
Commit“ message to
all workers.

3. Workers process
request and write
pending changes to
log.
•

If successful, worker
answers  „Ready“.
In case of errors, worker
answers „Failure“.

80

Two Phase Commit (2PC) – Commit Phase (success)

Log

Log

Worker

Worker

commit

• All workers answered

“ready”:
1. Coordinator sends “commit” to

all workers.

User

Coordinator

commit

Log

Log

Worker

Worker

81

Two Phase Commit (2PC) – Commit Phase (success)

Log

Log

Worker

Worker

ack

Response

User

Coordinator

ack

Log

Log

Worker

Worker

• All workers answered

“ready”:
1. Coordinator sends “commit”

to all workers.

2. Workers commit pending

changes from log;
Send “ack” to coordinator.

3. Coordinator completes

transaction, once all workers
finished.

82

Two Phase Commit (2PC) – Commit Phase (abort)

Log

Log

Worker

Worker

rollback

User

Coordinator

rollback

Log

Log

Worker

Worker

• All workers answered

“ready”:
1. Coordinator sends “commit”

to all workers.

2. Workers commit pending

changes from log;
Send “ack” to coordinator.

3. Coordinator completes
transaction, once all
workers finished.

• At least one

answered “failure”:
1. Coordinator sends

“rollback” to all workers.

83

Two Phase Commit (2PC) – Commit Phase (abort)

Log

Log

Worker

Worker

ack

Failure

User

Coordinator

ack

Log

Log

Worker

Worker

• All workers answered

“ready”:
1. Coordinator sends “commit”

to all workers.

2. Workers commit pending

changes from log;
Send “ack” to coordinator.

3. Coordinator completes
transaction, once all
workers finished.

• At least one

answered “failure”:
1. Coordinator sends

“rollback” to all workers.
2. Workers undo pending
changes from log;
Send “ack” to coordinator.

3. Coordinator undoes
transaction, once all
workers finished.

84

Two Phase Commit (2PC) – Failure Recovery

•

•

Simplest case: One (or more) recoverable worker failure.
– Assumption: Workers keep log of sent and received messages.

Failure occurs in Phase 1:
– Directly after receiving “Prepare” from Coordinator:

•

Transaction either timed out (failed) or Coordinator is still waiting. Make local decision and check with
Coordinator.

– After sending “Failure” to Coordinator:

•

The transaction has failed. Undo from log.

– After sending “Ready” to Coordinator:

•

The transaction might have succeeded. Check back with coordinator or neighboring nodes.

•

Failure occurs in Phase 2:
– After receiving “Abort” from Coordinator:

• Undo transaction from log.

85

Useful Reads

• Designing Data Intensive Apps, 2PL, 2PC
• Hector Garcia-Molina and Kenneth Salem., “Sagas”, SIGMOD '87).
• Rakesh Agrawal, Michael J. Carey, and Miron Livny: “Concurrency Control

Performance Modeling: Alternatives and Implications,” TODS ‘87

86

