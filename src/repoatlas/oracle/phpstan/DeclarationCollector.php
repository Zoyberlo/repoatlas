<?php

declare(strict_types=1);

namespace RepoAtlas\PhpStan;

use PhpParser\Node;
use PHPStan\Analyser\Scope;
use PHPStan\Collectors\Collector;

/**
 * Every definition the analysed code writes down, located by its name token.
 *
 * A resolved reference is only half a fact: the comparison this feeds is
 * anchored at source locations on both ends, so the target of a reference
 * has to be a definition somebody can point at. These are those targets.
 *
 * @implements Collector<Node, list<array<string, mixed>>>
 */
final class DeclarationCollector implements Collector
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
        if ($node instanceof Node\Stmt\ClassLike) {
            if ($node->name === null) {
                // `new class { ... }`: a definition with no name to anchor on.
                return null;
            }
            $fqn = $node->namespacedName?->toString() ?? $node->name->toString();
            return [$this->at($scope, $node->name, $this->classKind($node), $node->name->toString(), '', $fqn)];
        }
        $container = $this->container($scope);
        if ($node instanceof Node\Stmt\ClassMethod) {
            $kind = strtolower($node->name->toString()) === '__construct' ? 'constructor' : 'method';
            return [$this->at($scope, $node->name, $kind, $node->name->toString(), $container)];
        }
        if ($node instanceof Node\Stmt\Function_) {
            $fqn = $node->namespacedName?->toString() ?? $node->name->toString();
            return [$this->at($scope, $node->name, 'function', $node->name->toString(), '', $fqn)];
        }
        if ($node instanceof Node\Stmt\Property) {
            $records = [];
            foreach ($node->props as $property) {
                $records[] = $this->at($scope, $property->name, 'property', $property->name->toString(), $container);
            }
            return $records === [] ? null : $records;
        }
        if ($node instanceof Node\Stmt\ClassConst) {
            $records = [];
            foreach ($node->consts as $constant) {
                $records[] = $this->at($scope, $constant->name, 'constant', $constant->name->toString(), $container);
            }
            return $records === [] ? null : $records;
        }
        if ($node instanceof Node\Stmt\EnumCase) {
            return [$this->at($scope, $node->name, 'constant', $node->name->toString(), $container)];
        }
        return null;
    }

    private function classKind(Node\Stmt\ClassLike $node): string
    {
        if ($node instanceof Node\Stmt\Interface_) {
            return 'interface';
        }
        if ($node instanceof Node\Stmt\Trait_) {
            return 'trait';
        }
        if ($node instanceof Node\Stmt\Enum_) {
            return 'enum';
        }
        return 'class';
    }

    private function container(Scope $scope): string
    {
        $class = $scope->getClassReflection();
        return $class === null ? '' : $class->getName();
    }

    /**
     * The file a node is really in, which is not always the one being analysed.
     *
     * PHPStan analyses a trait's body once per class that uses it, with the
     * scope's file set to the *using class*. The nodes are still the
     * trait's, so their line numbers belong to the trait's file, and
     * pairing the two produced declarations at line 263 of four unrelated
     * files. On an application with shared traits that was 62,323 sites
     * whose target could not be placed.
     */
    private function fileOf(Scope $scope): string
    {
        $trait = $scope->getTraitReflection();
        if ($trait !== null) {
            $file = $trait->getFileName();
            if ($file !== null) {
                return $file;
            }
        }
        return $scope->getFile();
    }

    /**
     * A definition, with the name the resolver on the other side will use.
     *
     * `fqn` is what a resolved reference joins on: `App\Ad::save` for a
     * member, `App\Ad` for the class. A trait's method is analysed once per
     * class that uses it, so the same span arrives several times under
     * different names; that is not a duplicate to be dropped but the reason
     * the join works at all, since PHPStan reports the using class as the
     * declaring one.
     *
     * @return array<string, mixed>
     */
    private function at(
        Scope $scope,
        Node $node,
        string $kind,
        string $name,
        string $container,
        ?string $fqn = null
    ): array {
        return [
            'kind' => 'def',
            'file_of' => $this->fileOf($scope),
            'symbol_kind' => $kind,
            'name' => $name,
            'container' => $container,
            'fqn' => $fqn ?? ($container === '' ? $name : $container . '::' . $name),
            'line' => max(0, $node->getStartLine() - 1),
            'start' => $node->getStartFilePos(),
            'end' => $node->getEndFilePos() + 1,
        ];
    }
}
