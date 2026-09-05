<?php

declare(strict_types=1);

namespace RepoAtlas\PhpStan;

use PhpParser\Node;
use PHPStan\Analyser\Scope;
use PHPStan\Collectors\Collector;
use PHPStan\Reflection\ClassReflection;
use PHPStan\Type\Type;
use PHPStan\Type\VerbosityLevel;

/**
 * Every member access in the analysed code, with the type PHPStan resolved
 * for its receiver.
 *
 * This exists because of one measurement. On a Laravel application the
 * largest thing a tree-sitter index cannot resolve is a member reached
 * through a variable nobody declared a type for, and the second largest is
 * an Eloquent column, which has no declaration anywhere: the model invents
 * the property at run time from the table. No compiler front end sees the
 * second, which is why scip-php agrees with the index's blindness rather
 * than exposing it. PHPStan resolves the first properly and, with
 * larastan loaded, the second as well.
 *
 * One collector rather than one per node type: PHPStan's registry looks up
 * collectors through `class_implements`, and every node implements
 * PhpParser\Node, so registering for the interface fires for all of them
 * and the branching happens here where it is readable.
 *
 * @implements Collector<Node, list<array<string, mixed>>>
 */
final class SiteCollector implements Collector
{
    public function getNodeType(): string
    {
        return Node::class;
    }

    /**
     * @return list<array<string, mixed>>|null
     */
    public function processNode(Node $node, Scope $scope): ?array
    {
        $record = $this->site($node, $scope);
        return $record === null ? null : [$record];
    }

    /**
     * @return array<string, mixed>|null
     */
    private function site(Node $node, Scope $scope): ?array
    {
        if ($node instanceof Node\Expr\MethodCall || $node instanceof Node\Expr\NullsafeMethodCall) {
            return $this->member($node->name, $scope->getType($node->var), $scope, 'call', 'method');
        }
        if ($node instanceof Node\Expr\PropertyFetch || $node instanceof Node\Expr\NullsafePropertyFetch) {
            return $this->member($node->name, $scope->getType($node->var), $scope, 'property', 'property');
        }
        if ($node instanceof Node\Expr\StaticCall) {
            return $this->member($node->name, $this->classType($node->class, $scope), $scope, 'static_call', 'method');
        }
        if ($node instanceof Node\Expr\StaticPropertyFetch) {
            return $this->member($node->name, $this->classType($node->class, $scope), $scope, 'static_property', 'property');
        }
        if ($node instanceof Node\Expr\ClassConstFetch) {
            return $this->member($node->name, $this->classType($node->class, $scope), $scope, 'constant', 'constant');
        }
        if ($node instanceof Node\Expr\New_) {
            return $this->instantiation($node, $scope);
        }
        return null;
    }

    /**
     * The type named on the left of `::`, whether written out or `self`.
     */
    private function classType(Node $class, Scope $scope): Type
    {
        if ($class instanceof Node\Name) {
            return $scope->resolveTypeByName($class);
        }
        return $scope->getType($class);
    }

    /**
     * A member reached through a receiver, resolved or not.
     *
     * An unresolved site is recorded too, and deliberately: it is the only
     * honest way to state a ceiling. A report that lists what PHPStan found
     * without listing what it could not find says nothing about how much
     * was there to find.
     *
     * @return array<string, mixed>|null
     */
    private function member(
        Node|string $name,
        Type $receiver,
        Scope $scope,
        string $kind,
        string $member
    ): ?array {
        if (!$name instanceof Node\Identifier) {
            // `$object->$name()`: the member is itself an expression, and
            // no location in this file names it.
            return null;
        }
        $record = $this->at($name, $kind, $name->toString());
        $record['receiver'] = $receiver->describe(VerbosityLevel::typeOnly());

        // A member resolves through its receiver, or not at all. PHPStan
        // answers `yes` to hasMethod on `mixed`, so that it reports no error
        // where it knows nothing, and the declaring class it then hands back
        // is stdClass. Taken at face value that turned 3,910 sites on the
        // test application, forty-four percent of them, into resolutions
        // naming a class the code never mentions. Requiring the receiver to
        // name at least one class is the whole guard.
        if ($receiver->getObjectClassNames() === []) {
            $record['resolved'] = false;
            return $record;
        }

        $reflection = $this->lookup($receiver, $name->toString(), $member, $scope);
        if ($reflection === null) {
            $record['resolved'] = false;
            return $record;
        }
        [$declaring, $native] = $reflection;
        $record['resolved'] = true;
        $record['class'] = $declaring->getName();
        $record['file'] = $declaring->getFileName() ?? '';
        // Resolved, but written down nowhere: an Eloquent column, a __get,
        // a __call. The interesting half of this whole exercise, and the
        // half no SCIP indexer reports at all.
        $record['magic'] = !$native;
        return $record;
    }

    /**
     * The class a member was declared on, and whether it is written there.
     *
     * The line of the declaration is deliberately not read here. PHP's own
     * reflection has no line for a property or a class constant, and
     * PHPStan's wrapper for a member that does not exist in source throws
     * rather than answering. The consumer already has every declaration in
     * the analysed files with the exact span of its name token, so it joins
     * on the class and the member name and gets a better answer than a
     * start line would have been.
     *
     * @return array{ClassReflection, bool}|null
     */
    private function lookup(Type $receiver, string $name, string $member, Scope $scope): ?array
    {
        if ($member === 'method') {
            if (!$receiver->hasMethod($name)->yes()) {
                return null;
            }
            $declaring = $receiver->getMethod($name, $scope)->getDeclaringClass();
            return [$declaring, $declaring->hasNativeMethod($name)];
        }
        if ($member === 'property') {
            if (!$receiver->hasProperty($name)->yes()) {
                return null;
            }
            $declaring = $receiver->getProperty($name, $scope)->getDeclaringClass();
            return [$declaring, $declaring->hasNativeProperty($name)];
        }
        if (!$receiver->hasConstant($name)->yes()) {
            return null;
        }
        $declaring = $receiver->getConstant($name)->getDeclaringClass();
        return [$declaring, $declaring->hasConstant($name)];
    }

    /**
     * @return array<string, mixed>|null
     */
    private function instantiation(Node\Expr\New_ $node, Scope $scope): ?array
    {
        if (!$node->class instanceof Node\Name) {
            return null;
        }
        $type = $scope->resolveTypeByName($node->class);
        $record = $this->at($node->class, 'new', $node->class->toString());
        $record['receiver'] = $type->describe(VerbosityLevel::typeOnly());
        $names = $type->getObjectClassNames();
        if ($names === []) {
            $record['resolved'] = false;
            return $record;
        }
        $reflections = $type->getObjectClassReflections();
        $declaring = $reflections[0] ?? null;
        $record['resolved'] = $declaring !== null;
        if ($declaring !== null) {
            $record['class'] = $declaring->getName();
            $record['file'] = $declaring->getFileName() ?? '';
            $record['magic'] = false;
        }
        return $record;
    }

    /**
     * The location of the token that names the thing, in bytes.
     *
     * Byte offsets rather than columns because the index this is compared
     * against counts UTF-8 bytes, and converting once on the Python side
     * from a file it has already read is cheaper and less error-prone than
     * two producers each guessing at an encoding.
     *
     * @return array<string, mixed>
     */
    private function at(Node $node, string $kind, string $name): array
    {
        return [
            'kind' => $kind,
            'name' => $name,
            'line' => max(0, $node->getStartLine() - 1),
            'start' => $node->getStartFilePos(),
            'end' => $node->getEndFilePos() + 1,
            'receiver' => '',
            'resolved' => false,
            'class' => '',
            'file' => '',
            'magic' => false,
        ];
    }
}
