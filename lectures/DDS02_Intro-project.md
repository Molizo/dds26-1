DDS Project

Asterios Katsifodimos

A tale of three Cloud services

Order
service

Payment
service

Stock
service

Service
(stateless) layer

Datab
ase

Datab
ase

Datab
ase

Database
(stateful) layer

To checkout: stock & update stock, verify payment, checkout the cart. Atomically!

2

Services Architecture (1): Easiest Implem.

Order
Business
Logic

l
l

a
C
T
S
E
R

Payment
Business
Logic

REST Call

Stock
Business
Logic

DB

DB

DB

▪ Perform an order iff there
is stock available and the
payment is cleared.

▪
Services are stateless
▪ Database does the heavy-

lifting

▪ High latency, costly state

access

▪ No guaranteed messaging

3

Services Architecture (2): Embedded State/DB

REST Call

Stock
Business
Logic

DB

▪

Low-latency access to
local state

Service calls still expensive

▪
▪ Messaging still not

guaranteed

▪ Not obvious how to scale

this out

▪

Fault tolerance is hard!

Order
Business
Logic

DB

l
l

a
C
T
S
E
R

Payment
Business
Logic

DB

4

Services Architecture (3): Event Sourcing

REST Call
event-log

Stock
Business
Logic

DB

Order
Business
Logic

DB

e
v
e
n
t
-
l
o
g

l
l

a
C
T
S
E
R

Payment
Business
Logic

DB

▪ Message exchange

through an event-log
▫ Guaranteed at-least

once delivery!

Services are
asynchronous/reactive.

If we lose state, we replay
the log and rebuild it.

Time-travel debugging,
audits, etc. are easier.

▪

▪

▪

▪ Does not scale!

5

Services Architecture (4): Scalable Deployment

Order 1

Business
Logic

Subscribe for  Responses

DB

Order 2

Business
Logic

DB

event-log

event-log

event-log

Stock 1

Business
Logic

DB

Stock 2

Business
Logic

DB

Payment 1
Business
Logic

DB

Order 3

RPC Calls

Business
Logic

DB

6

Services Architecture (4): Scalable Deployment

Order 1

Business
Logic

Subscribe for  Responses

DB

Order 2

Business
Logic

DB

event-log

event-log

event-log

Stock 1

Business
Logic

DB

Stock 2

Business
Logic

DB

Payment 1
Business
Logic

DB

Order 3

RPC Calls

Business
Logic

DB

7

Project
Previously part of “Web-scale Data Management” (2020 - 2024)

Challenge: implement three Cloud service Stock, Order,
Payment.

Goal: >10K per second order checkouts, without losing money
or stock.

Few teams managed so far.

https://docs.google.com/document/d/1OAHOSXucovy6
m1RG_4UgwMbVr7dED9Xng5Bqk5ftgVw/edit?tab=t.0#
heading=h.vktzhx951fsy

8

Why so hard?
state, messaging & app logic cannot be treated separately.

Conjecture 1: unless runtime fully controls state & messaging, exactly-once is impossible.
Main reason: state mutations are causally dependent on messages.
Corollary: unless runtime guarantees exactly-once, failures will “leak” to the app logic.
Conjecture 2: unless runtime offers transaction primitives, transactions will “leak” to app logic.

Order
service

Payment
service

Stock
service

App. logic & messaging layer

Datab
ase

Datab
ase

Datab
ase

Application State

9

A 40-year old problem: the Byzantine Generals Problem

L. Lamport, R. Shostak, and M. Pease.
In the ACM Transactions on Programming Languages and Systems (1982).

Example:

checkout a shopping cart checkout
Cloud services
“Two generals cannot agree on a time to attack a city,
if they are using a messenger that can be killed on the way.”

unreliable Cloud machines / network.

10

Scalable Cloud Application development is hard!

Cloud Computing

Elasticity, costs, operations,
continuous deployment, storage, etc.

Debugging, clean code,
algorithms, data structures, etc.

Programming

Distributed
systems

Scalability, parallelism, messaging
fault tolerance, consistency, etc.

Transactions, ACIDity, etc.

Database
Systems

              Domain
              Knowledge

Business logic, requirements, etc.

*this Venn diagram does not represent set-sizes properly.

Scalable Cloud Application development is hard!

Cloud Computing

Elasticity, costs, operations,
continuous deployment, storage, etc.

Debugging, clean code,
algorithms, data structures, etc.

Programming

Distributed
systems

Scalability, parallelism, messaging
fault tolerance, consistency, etc.

Transactions, ACIDity, etc.

Database
Systems

              Domain
              Knowledge

Business logic, requirements, etc.

People who can develop
scalable Cloud applications.

*this Venn diagram does not represent set-sizes properly.

Wait, what about serverless? That should work!

Managed
Infrastructure
(autoscaling, no ops)

Function-based
programming
model

VM
Fn

Fn

Fn

VM
Fn

Fn

Fn

VM
Fn

Fn

Fn

Cloud database

No State

Fn-to-fn calls

Transactions

13

We live in the stone ages of Cloud
computing.
Building scalable Cloud applications
is like programming assembly before
compilers were around.

This course makes sure you realize
this, and learn how to work around
it.

“Two-pizza” dev team in the year 2026.

14

In the meantime

15

Styx

https://github.com/delftdata/styx

s
e
c
i

o
h
C
n
g
i
s
e
D

Object-oriented API

“Keep the data moving” [1]

Performance

Dataflows are awkward.
Styx offers Durable Entities [4],
i.e., Python objects with arbitrary
function-to-function calls & state.

No Two-phase Commits. Styx
extends deterministic database
concepts [2] for arbitrary function-
to-function calls.

Partitioned state, collocated with
app code. Parallel execution at
Entity-level granularity.

Coarse-grained Fault Tolerance

Exactly-once output

Consistency

Async checkpoints á lá Chandy-
Lamport [3,4].

Uses durable queues for input
transactions, and  deduplicating
outputs.

Serializable state mutations made
by arbitrary function-to-function
calls.

[1] Stonebraker, Çetintemel, Zdonik. "The 8 requirements of real-time stream processing." [Sig. Record 2005].
[2]  Yi, Yu, Cao, and Samuel Madden. "Aria: a fast and practical deterministic OLTP database."  [VLDB 2020].
[3] Carbone, et.al. "State management in Apache Flink®: consistent stateful distributed stream processing." [VLDB 2018].
[4] Silvestre, Fragkoulis, Katsifodimos. "Clonos: Consistent causal recovery for highly-available streaming dataflows." [SIGMOD 21]
[4] Psarakis, Zorgdrager, Fragkoulis, Salvanesci, Katsifodimos “Stateful Entities: Object-oriented Cloud Applications as Distributed Dataflows”, [CIDR ‘23, EDBT ’24]
[5] Psarakis, Christodoulou, Siachamis, Fragkoulis, Katsifodimos “Styx: Transactional Stateful Functions on Streaming Dataflows”, [SIGMOD’25]

16

Styx: A novel dataflow system for transactional Cloud apps

Durable Entities API

Python Object-oriented API. App code resembles
single-machine code (with some conventions).

Operator API

Dataflow Execution Engine

Python–based, access to lower-level
operator primitives for advanced programmers.

Serializability, exactly-once output, early commit-
replies, low-latency processing

LSM Storage
(RocksDB)

Transactional
Protocol

Fault
Tolerance

Auto-scaling
(WiP)

Cloud Provider

Google Cloud

Resource Manager
(Kubernetes, …)

Message Broker
(Kafka, RedPanda)

Blob Storage
(AWS S3)

17

This is not it

This year’s project is more complex

▪

▪

Last year’s projects were amazing! (thank you ChatGPT)

This year we embrace GenAI, but:
▫ Harder Project
▫ More to learn…by generalizing!

▪ Plan

Till midterm: Implement the three microservices using GenAI (also
test them and benchmark them)
Till end-term: implement an “orchestrator service” like Temporal.io,
Restate.io, Uber’s Cadence, AWS’/Azure’s Durable Functions

19

